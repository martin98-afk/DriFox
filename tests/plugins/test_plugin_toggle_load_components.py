# -*- coding: utf-8 -*-
"""内置插件启停去双通道（B 项）：load_components 参数 + 主线程全组件补载

== 背景 ==
worker 线程 pm.enable_plugin/disable_plugin 完整加载/卸载全部组件（含 UI
load_plugin 完整 exec），主线程 _on_manage_done 又补载一轮——UI 组件被完整
exec 两轮，禁用对称双跑。

== 修复口径 ==
1. enable_plugin/disable_plugin 加 load_components=True（默认语义不变，
   user-custom 自动创建与 shortcut-manager 路径零改动）；市场 installer
   _set_managed_enabled 传 False（worker 只写配置）。
2. 🔴 主线程补载必须全组件口径（__manifest__/清理段），严禁只补 UI 单组件
   ——历史上只清/补 UI+tools 时 9 类非 UI 组件残留或缺失，无自愈只能重启。
3. plugin_changed 后置到补载完成后 emit（避免「已启用但组件未就绪」中间态）。

== 断言策略 ==
- plugin_manager 侧行为断言（stub 实例直调，覆盖默认/False 两口径 + 快速连切）
- cards/installer 侧 AST 口径断言（全组件双分支 + emit 后置 + False 传参 + 
  user-custom 默认路径未被改动）
"""

import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt5.QtWidgets")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ═══════════════════════════════════════════════════════════════
# plugin_manager 行为断言
# ═══════════════════════════════════════════════════════════════

def _make_pm(monkeypatch, name="agent_trace", initially_enabled=True):
    """构造最小 PluginManager stub（绕过 __init__，注入启停依赖）"""
    from app.plugins.managers.plugin_manager import PluginManager

    inst = PluginManager.__new__(PluginManager)
    plugin = MagicMock()
    plugin.name = name
    inst._plugins = {name: plugin}
    inst._enabled = {name} if initially_enabled else set()
    inst._disabled = set() if initially_enabled else {name}
    inst._load_calls = []
    inst._unload_calls = []
    inst._hooks = []

    monkeypatch.setattr(inst, "_get_enabled_set", lambda: set(inst._enabled))
    monkeypatch.setattr(inst, "_save_enabled_set", lambda s: setattr(inst, "_enabled", set(s)))
    monkeypatch.setattr(inst, "_get_disabled_set", lambda: set(inst._disabled))
    monkeypatch.setattr(inst, "_save_disabled_set", lambda s: setattr(inst, "_disabled", set(s)))
    monkeypatch.setattr(inst, "invalidate_mcp_cache", lambda: None)
    monkeypatch.setattr(
        inst, "_load_plugin_components", lambda n: inst._load_calls.append(n)
    )
    monkeypatch.setattr(
        inst, "_unload_plugin_components", lambda n: inst._unload_calls.append(n)
    )
    monkeypatch.setattr(
        inst,
        "_trigger_plugin_changed_hook",
        lambda action, target, extra=None: inst._hooks.append((action, target)),
    )
    return inst


def test_enable_default_loads_components_and_hook(monkeypatch):
    """默认参数路径语义不变：加载全部组件 + plugin_changed hook（user-custom 依赖）"""
    inst = _make_pm(monkeypatch, initially_enabled=False)
    inst.enable_plugin("agent_trace")
    assert inst._load_calls == ["agent_trace"], "默认路径必须联动加载全部组件"
    assert inst._hooks == [("enabled", "agent_trace")], "默认路径必须在加载后触发 hook"
    assert "agent_trace" in inst._enabled


def test_enable_load_components_false_defers_everything(monkeypatch):
    """load_components=False：只写配置，不加载组件、不触发 hook（主线程补载链负责）"""
    inst = _make_pm(monkeypatch, initially_enabled=False)
    inst.enable_plugin("agent_trace", load_components=False)
    assert inst._load_calls == [], "False 口径不得在 worker 加载组件"
    assert inst._hooks == [], "False 口径不得在 worker 触发 hook（后置到补载完成）"
    assert "agent_trace" in inst._enabled, "配置必须已写入"
    assert "agent_trace" not in inst._disabled, "对称双写：禁用集必须移除"


def test_disable_default_unloads_and_hook(monkeypatch):
    """默认路径语义不变：卸载全部组件 + hook（完整禁用语义）"""
    inst = _make_pm(monkeypatch, initially_enabled=True)
    inst.disable_plugin("agent_trace")
    assert inst._unload_calls == ["agent_trace"], "默认路径必须联动卸载全部组件"
    assert inst._hooks == [("disabled", "agent_trace")], "默认路径必须在卸载后触发 hook"
    assert "agent_trace" not in inst._enabled
    assert "agent_trace" in inst._disabled


def test_disable_load_components_false_defers_everything(monkeypatch):
    """load_components=False：只写配置，不卸载组件、不触发 hook"""
    inst = _make_pm(monkeypatch, initially_enabled=True)
    inst.disable_plugin("agent_trace", load_components=False)
    assert inst._unload_calls == [], "False 口径不得在 worker 卸载组件"
    assert inst._hooks == [], "False 口径不得在 worker 触发 hook"
    assert "agent_trace" not in inst._enabled, "配置必须已写入"
    assert "agent_trace" in inst._disabled, "对称双写：禁用集必须加入"


def test_rapid_disable_enable_final_state_consistent(monkeypatch):
    """快速连续禁用→启用（False 口径）：最终态一致，worker 全程零 hook 零装卸"""
    inst = _make_pm(monkeypatch, initially_enabled=True)
    inst.disable_plugin("agent_trace", load_components=False)
    inst.enable_plugin("agent_trace", load_components=False)
    assert "agent_trace" in inst._enabled, "最终态：已启用"
    assert "agent_trace" not in inst._disabled, "最终态：禁用集已移除"
    assert inst._load_calls == [] and inst._unload_calls == [], "双通道拆除：worker 全程零装卸"
    assert inst._hooks == [], "hook 全部后置到主线程补载完成"


def test_rapid_toggle_idempotent_second_pass(monkeypatch):
    """重复启用（state 不变）：幂等跳过，配置与 hook 均无副作用"""
    inst = _make_pm(monkeypatch, initially_enabled=False)
    inst.enable_plugin("agent_trace", load_components=False)
    first_enabled = set(inst._enabled)
    inst.enable_plugin("agent_trace", load_components=False)
    assert inst._enabled == first_enabled, "重复启用不得改变状态"


# ═══════════════════════════════════════════════════════════════
# cards / installer AST 口径断言
# ═══════════════════════════════════════════════════════════════

def _load_method(path: Path, cls_name: str, method_name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.Module)):
            body = node.body if isinstance(node, ast.ClassDef) else node.body
            if isinstance(node, ast.ClassDef) and node.name != cls_name:
                continue
            for item in body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return item
    raise AssertionError(f"未找到 {cls_name}.{method_name}")


def _source(method: ast.FunctionDef) -> str:
    return ast.unparse(method)


def test_manage_done_uses_full_component_reload():
    """_on_manage_done 补载必须走全组件口径方法（严禁单 UI 组件补载回归）"""
    path = _REPO_ROOT / "plugins" / "plugin-marketplace" / "ui" / "cards.py"
    method = _load_method(path, "MarketplaceCard", "_on_manage_done")
    src = _source(method)
    assert "_reload_plugin_components_on_gui" in src, (
        "启停补载必须走 _reload_plugin_components_on_gui（全组件口径）"
    )
    assert "_reload_plugin_ui_on_gui(name)" not in src, (
        "启停补载严禁走 _reload_plugin_ui_on_gui（单 UI 组件口径会残留 9 类非 UI 组件）"
    )
    assert "enabled=kind == 'enable'" in src, (
        "补载必须区分启用/禁用方向"
    )


def test_reload_plugin_components_on_gui_full_scope():
    """补载方法必须走能力池通道（caps_for）+ None 回退，零直接导入。"""
    path = _REPO_ROOT / "plugins" / "plugin-marketplace" / "ui" / "cards.py"
    method = _load_method(path, "MarketplaceCard", "_reload_plugin_components_on_gui")
    src = _source(method)
    assert "caps_for('plugin.reload_components')" in src or 'caps_for("plugin.reload_components")' in src, (
        "补载必须走能力池 plugin.reload_components 通道（架构红线：插件禁直接 import 主程序包）"
    )
    assert "is None" in src, "能力不可用（未声明/未注册）时必须回退跳过，不得另创通道"
    assert "reload_components(name, enabled)" in src, "必须透传 (name, enabled) 全组件口径"
    for banned in ("_reload_single_plugin", "_cleanup_removed_plugin_components", "emit_plugin_changed", "PluginHostService"):
        assert banned not in src, f"插件侧不得直接触达宿主私有符号: {banned}（已收口能力池）"


def test_host_provider_full_component_semantics(monkeypatch):
    """宿主 provider 行为：enable 走 __manifest__ 全组件分派、disable 走全组件清理段、
    emit_plugin_changed 后置（action=enabled/disabled），私有性封装在宿主侧。"""
    from app.plugins.capabilities.providers import plugin_host

    api = plugin_host.reload_components(None)
    assert callable(api)

    svc = MagicMock()
    svc._reload_single_plugin.return_value = {"ui": True}
    monkeypatch.setattr(
        "app.core.services.plugin_host_service.PluginHostService.get_instance",
        staticmethod(lambda: svc),
    )
    result = api("agent_trace", True)
    assert result == {"ui": True}
    svc._reload_single_plugin.assert_called_once_with("agent_trace", "__manifest__")
    svc.emit_plugin_changed.assert_called_once()
    emit_args = svc.emit_plugin_changed.call_args
    assert emit_args.args[1] == "agent_trace" and emit_args.kwargs.get("action") == "enabled"

    # disable：全组件清理段 + action=disabled 后置 emit
    pm = MagicMock()
    plugin = MagicMock()
    plugin.components = {"tools": True}
    plugin.manifest = {"name": "agent_trace"}
    pm.get_plugin.return_value = plugin
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: pm),
    )
    svc2 = MagicMock()
    monkeypatch.setattr(
        "app.core.services.plugin_host_service.PluginHostService.get_instance",
        staticmethod(lambda: svc2),
    )
    api2 = plugin_host.reload_components(None)
    result2 = api2("agent_trace", False)
    svc2._cleanup_removed_plugin_components.assert_called_once()
    cleanup_args = svc2._cleanup_removed_plugin_components.call_args.args
    assert cleanup_args[0] == "agent_trace" and cleanup_args[1] == {"tools": True}
    svc2.emit_plugin_changed.assert_called_once()
    assert svc2.emit_plugin_changed.call_args.kwargs.get("action") == "disabled"
    assert isinstance(result2, dict)


def test_capability_key_registered_and_declared():
    """能力键注册冒烟：registry 有实现、catalog 有声明、manifest 已裁剪声明。

    坑检查（review 指定）：capabilities 丢键按空裁剪——插件 manifest 少写键 →
    caps_for 拿空。三处必须同时存在。
    """
    from app.plugins.capabilities.catalog import CATALOG
    from app.plugins.capabilities.providers import ui_core
    from app.plugins.capabilities.registry import CapabilityRegistry

    assert "plugin.reload_components" in CATALOG, "catalog 声明缺失（丢键按空裁剪）"

    reg = CapabilityRegistry()
    ui_core.register(reg)
    assert "plugin.reload_components" in reg.registered("ui"), (
        "ui_core.register 未登记 plugin.reload_components 实现"
    )
    assert reg.has("plugin.reload_components", "ui"), "能力键 ui surface 可用性断言失败"

    manifest = (_REPO_ROOT / "plugins" / "plugin-marketplace" / ".drifox-plugin" / "plugin.json").read_text(
        encoding="utf-8"
    )
    assert "plugin.reload_components" in manifest, "marketplace manifest 未声明新键（会被裁剪拿空）"


def test_set_managed_enabled_passes_load_components_false():
    """installer _set_managed_enabled 必须传 load_components=False（worker 只写配置）"""
    path = _REPO_ROOT / "plugins" / "plugin-marketplace" / "ui" / "installer.py"
    method = _load_method(path, "PluginInstaller", "_set_managed_enabled")
    src = _source(method)
    assert src.count("load_components=False") == 2, (
        "enable/disable 两处都必须传 load_components=False（worker 只写配置）"
    )


def test_user_custom_default_path_unchanged():
    """user-custom 自动创建路径保持默认参数（完整语义：加载组件 + hook）"""
    path = _REPO_ROOT / "plugins" / "shortcut-manager" / "ui" / "cards.py"
    src = path.read_text(encoding="utf-8")
    assert 'pm.enable_plugin("user-custom")' in src, "user-custom 默认路径不得改动"
    assert "load_components" not in src, "user-custom 路径不应感知 load_components 参数"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
