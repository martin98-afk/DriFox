# -*- coding: utf-8 -*-
"""Task 8 回归测试：provider 安装刷新链 + .drifox-plugin manifest 变更映射

覆盖三项修复：
- _reload_new_plugin 必须加载 tools/providers/team_templates 三个新组件
  （走 kernel 注册表，与 builtin_reloaders 一致；不再沉默丢失）
- ROOT_FILE_COMPONENTS 增加 `.drifox-plugin` → "__manifest__" sentinel
  （触发 _reload_single_plugin 全组件重载：manifest 变更 = 重新探测组件差异）
- 旧 `app.core.plugin_manager` import 修正后该模块可被 tests/plugins/* 路径下测试
  沿用现有 fixture 触发 collection
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ════════════════════════════════════════════════════════════════════════
# 1) _reload_new_plugin：providers/tools/team_templates 加载链
# ════════════════════════════════════════════════════════════════════════


def _build_backend():
    """绕过 Qt 初始化构造 ChatBackend 实例"""
    from app.core.backend import ChatBackend

    backend = ChatBackend.__new__(ChatBackend)
    backend._agent_manager = None
    return backend


def _install_pm(monkeypatch, components: dict):
    """安装 fake PluginManager：返回含给定 components 的插件"""
    fake_plugin = MagicMock()
    fake_plugin.components = components
    fake_plugin.has_component = lambda c: bool(components.get(c))
    fake_plugin.path = MagicMock()

    fake_pm = MagicMock()
    fake_pm.is_initialized.return_value = True
    fake_pm.get_plugin.return_value = fake_plugin
    fake_pm.rescan_plugin = MagicMock()

    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )
    return fake_pm, fake_plugin


def test_reload_new_plugin_invokes_providers_watcher_scan(monkeypatch):
    """新插件含 providers 组件 → provider watcher 对该插件执行精准 reload_plugin

    builtin_reloaders 走精准 reload 路径（不再触发 scan_now 全量重扫），
    见 app/plugins/builtin_reloaders.py::_reload_providers。
    """
    backend = _build_backend()
    _install_pm(monkeypatch, {"providers": True})

    reload_calls = []
    fake_watcher = MagicMock()
    fake_watcher.reload_plugin = lambda name: reload_calls.append(name)

    monkeypatch.setattr(
        "app.plugins.loaders.provider_loader.ensure_provider_watcher",
        lambda: fake_watcher,
    )

    result = backend._reload_new_plugin("my-provider-plugin")

    assert reload_calls == ["my-provider-plugin"], (
        f"providers reload_plugin 应被调用一次针对该插件，实际 {reload_calls}"
    )
    assert result["providers"] is True, f"result['providers'] 应为 True，实际 {result.get('providers')}"


def test_reload_new_plugin_invokes_tools_watcher_scan(monkeypatch):
    """新插件含 tools 组件 → tool watcher 对该插件执行精准 reload_plugin + 通知

    builtin_reloaders 走精准 reload 路径（不再触发 scan_now 全量重扫），
    见 app/plugins/builtin_reloaders.py::_reload_tools。
    """
    backend = _build_backend()
    _install_pm(monkeypatch, {"tools": True})

    reload_calls = []
    notify_calls = []
    fake_watcher = MagicMock()
    fake_watcher.reload_plugin = lambda name: reload_calls.append(name)
    fake_watcher._notify_reloaded = lambda: notify_calls.append(1)

    monkeypatch.setattr(
        "app.plugins.loaders.plugin_tool_loader.ensure_plugin_tool_watcher",
        lambda: fake_watcher,
    )

    result = backend._reload_new_plugin("my-tools-plugin")

    assert reload_calls == ["my-tools-plugin"], (
        f"tools reload_plugin 应被调用一次针对该插件，实际 {reload_calls}"
    )
    assert notify_calls == [1], f"_notify_reloaded 应被调用一次，实际 {notify_calls}"
    assert result["tools"] is True, f"result['tools'] 应为 True，实际 {result.get('tools')}"


def test_reload_new_plugin_marks_team_templates(monkeypatch):
    """新插件含 team_templates 组件 → result['team_templates'] = True（懒加载语义）"""
    backend = _build_backend()
    _install_pm(monkeypatch, {"team_templates": True})

    result = backend._reload_new_plugin("my-team-plugin")

    assert result["team_templates"] is True, (
        f"team_templates 应被标记 True，实际 {result.get('team_templates')}"
    )


def test_reload_new_plugin_skips_components_when_absent(monkeypatch):
    """新插件不声明 providers/tools/team_templates → 三个组件均不触发 reload_plugin"""
    backend = _build_backend()
    _install_pm(monkeypatch, {"agents": True})

    provider_reload = []
    tool_reload = []
    monkeypatch.setattr(
        "app.plugins.loaders.provider_loader.ensure_provider_watcher",
        lambda: MagicMock(reload_plugin=lambda name: provider_reload.append(name)),
    )
    monkeypatch.setattr(
        "app.plugins.loaders.plugin_tool_loader.ensure_plugin_tool_watcher",
        lambda: MagicMock(
            reload_plugin=lambda name: tool_reload.append(name),
            _notify_reloaded=lambda: None,
        ),
    )

    # agents 路径会触发 reload_plugin_agents → 给 fake agent_manager 桩
    fake_am = MagicMock()
    fake_am.reload_plugin_agents = lambda name: 0
    backend._agent_manager = fake_am

    result = backend._reload_new_plugin("agents-only")

    assert provider_reload == [], f"无 providers 组件不应调 reload_plugin，实际 {provider_reload}"
    assert tool_reload == [], f"无 tools 组件不应调 reload_plugin，实际 {tool_reload}"
    assert result["providers"] is False
    assert result["tools"] is False
    assert result["team_templates"] is False


def test_reload_new_plugin_does_not_log_missing_lsp_key():
    """_reload_new_plugin 日志字段补全 tools/providers/team_templates（防 KeyError 回归）

    Task 7 之前 result dict 已迁移至 KNOWN_COMPONENTS 动态生成；但日志拼接使用 result['xxx']
    会 KeyError。模拟一次含 providers 的调用，确认不抛 KeyError。
    """
    backend = _build_backend()
    _install_pm(monkeypatch := pytest.MonkeyPatch(), {"providers": True})

    fake_watcher = MagicMock()
    fake_watcher.scan_now = lambda: None
    monkeypatch.setattr(
        "app.plugins.loaders.provider_loader.ensure_provider_watcher",
        lambda: fake_watcher,
    )

    # 不应抛 KeyError（result dict 含 tools/providers/team_templates 三键）
    result = backend._reload_new_plugin("logger-test")
    assert "providers" in result
    assert "tools" in result
    assert "team_templates" in result


# ════════════════════════════════════════════════════════════════════════
# 2) .drifox-plugin manifest 变更映射 → "__manifest__" sentinel
# ════════════════════════════════════════════════════════════════════════


def test_root_file_components_contains_drifox_plugin_manifest():
    """ROOT_FILE_COMPONENTS 必须含 .drifox-plugin → "__manifest__" 映射"""
    from app.plugins import kernel

    assert ".drifox-plugin" in kernel.ROOT_FILE_COMPONENTS, (
        "ROOT_FILE_COMPONENTS 必须含 .drifox-plugin 映射，否则 manifest 变更不触发重载"
    )
    assert kernel.ROOT_FILE_COMPONENTS[".drifox-plugin"] == "__manifest__", (
        "manifest 必须映射到 __manifest__ sentinel（特殊标记，走全组件重载）"
    )


def test_identify_components_recognizes_manifest_change(tmp_path):
    """_identify_all_components_from_changes 识别 .drifox-plugin/plugin.json → __manifest__"""
    from app.core.backend import ChatBackend

    backend = ChatBackend.__new__(ChatBackend)
    plugin_dir = tmp_path / "my-plugin"
    manifest_dir = plugin_dir / ".drifox-plugin"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "plugin.json").write_text('{"name": "my-plugin"}', encoding="utf-8")

    plugin_prefixes = {str(plugin_dir).lower(): "my-plugin"}
    changes = [(None, str(manifest_dir / "plugin.json"))]
    comps = backend._identify_all_components_from_changes(changes, plugin_prefixes, "my-plugin")

    assert "__manifest__" in comps, f"manifest 变更应被识别为 __manifest__，实际 comps={comps}"


def test_identify_components_fallback_recognizes_manifest_change(tmp_path):
    """fallback 路径（plugin_prefixes 索引过期）也能识别 .drifox-plugin 变更"""
    from pathlib import Path

    from app.core.backend import ChatBackend
    from app.plugins.managers.plugin_manager import PluginInfo

    plugin_dir = tmp_path / "my-plugin"
    plugin_dir.mkdir()
    manifest_dir = plugin_dir / ".drifox-plugin"
    manifest_dir.mkdir()
    (manifest_dir / "plugin.json").write_text('{"name": "my-plugin"}', encoding="utf-8")

    # 用真实 PluginInfo + 真实路径（fallback 内部 str(plugin.path.resolve())）
    real_plugin = PluginInfo(name="my-plugin", manifest={}, path=Path(plugin_dir))

    fake_pm = MagicMock()
    fake_pm.get_plugin.return_value = real_plugin
    fake_pm.is_initialized.return_value = True

    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )

    backend = ChatBackend.__new__(ChatBackend)
    changes = [(None, str(manifest_dir / "plugin.json"))]
    comps = backend._identify_components_from_changes_fallback("my-plugin", changes)

    assert "__manifest__" in comps, f"fallback 也应识别 manifest 变更为 __manifest__，实际 {comps}"
    monkey.undo()


def test_reload_single_plugin_manifest_triggers_all_components(monkeypatch, tmp_path):
    """component="__manifest__" → rescan + 遍历该插件全部已声明 components 调 registry.reload

    manifest 变更 = 组件清单可能增删，必须全组件重载以重新探测差异。
    """
    from app.core.backend import ChatBackend
    from app.plugins.kernel import ComponentReloaderRegistry, ReloadContext, get_reloader_registry

    # 插件声明了 3 个组件
    fake_plugin = MagicMock()
    fake_plugin.components = {"agents": True, "commands": True, "ui": True}
    fake_plugin.has_component = lambda c: bool(fake_plugin.components.get(c))
    fake_plugin.path = tmp_path

    fake_pm = MagicMock()
    fake_pm.is_initialized.return_value = True
    fake_pm.get_plugin.return_value = fake_plugin
    fake_pm.rescan_plugin = MagicMock()

    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )

    # 注册表：捕获所有调用
    reg = ComponentReloaderRegistry()
    calls = []
    reg.register("agents", lambda ctx: calls.append(("agents", ctx.plugin_name)) or 1)
    reg.register("commands", lambda ctx: calls.append(("commands", ctx.plugin_name)) or True)
    reg.register("ui", lambda ctx: calls.append(("ui", ctx.plugin_name)) or True)
    monkeypatch.setattr("app.plugins.kernel.get_reloader_registry", lambda: reg)

    backend = ChatBackend.__new__(ChatBackend)
    backend._agent_manager = None

    result = backend._reload_single_plugin("manifest-plugin", "__manifest__")

    # rescan 必须被调用一次（探测新 components）
    assert fake_pm.rescan_plugin.call_count == 1
    assert fake_pm.rescan_plugin.call_args.args == ("manifest-plugin",)
    # 三个已声明组件全部被调
    called_components = {c for c, _ in calls}
    assert called_components == {"agents", "commands", "ui"}, (
        f"manifest 重载必须遍历该插件全部已声明 components，实际调用 {called_components}"
    )
    # result 字段对齐
    assert result["agents"] == 1
    assert result["commands"] is True
    assert result["ui"] is True


def test_reload_single_plugin_manifest_skips_unknown_component(monkeypatch, tmp_path):
    """manifest 触发的全组件重载：仅遍历该插件实际声明的 components，不调未声明的"""
    from app.core.backend import ChatBackend
    from app.plugins.kernel import ComponentReloaderRegistry

    fake_plugin = MagicMock()
    fake_plugin.components = {"ui": True}  # 只声明了 ui
    fake_plugin.has_component = lambda c: bool(fake_plugin.components.get(c))
    fake_plugin.path = tmp_path

    fake_pm = MagicMock()
    fake_pm.is_initialized.return_value = True
    fake_pm.get_plugin.return_value = fake_plugin
    fake_pm.rescan_plugin = MagicMock()

    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )

    reg = ComponentReloaderRegistry()
    calls = []
    # 注册全部 11 类（含未声明的 tools/providers）— 只 ui 应被调
    for comp in (
        "agents",
        "hooks",
        "commands",
        "themes",
        "skills",
        "mcp",
        "lsp",
        "ui",
        "tools",
        "providers",
        "team_templates",
    ):
        reg.register(comp, lambda ctx, c=comp: calls.append(c) or True)
    monkeypatch.setattr("app.plugins.kernel.get_reloader_registry", lambda: reg)

    backend = ChatBackend.__new__(ChatBackend)
    backend._agent_manager = None

    backend._reload_single_plugin("only-ui", "__manifest__")

    assert calls == ["ui"], f"仅声明 ui 的插件走 manifest 重载只应调 ui，实际 {calls}"


# ════════════════════════════════════════════════════════════════════════
# 3) 旧 plugin_manager import 修正：collection 可成功
# ════════════════════════════════════════════════════════════════════════


def test_plugin_manager_importable_from_new_path():
    """旧导入路径 app.core.plugin_manager 已废，新路径 app.plugins.managers.plugin_manager 必须可用"""
    import app.core as core_pkg

    # 旧路径 app.core.plugin_manager 在重构时已删除，core_pkg 不应再导出该名字
    assert not hasattr(core_pkg, "plugin_manager") or not hasattr(
        getattr(core_pkg, "plugin_manager", None), "PluginManager"
    ), "app.core.plugin_manager 不应再含 PluginManager（已迁 app.plugins.managers.plugin_manager）"

    # 新路径必须可用
    from app.plugins.managers.plugin_manager import PluginManager

    assert hasattr(PluginManager, "get_instance")


def test_marketplace_update_reload_ui_module_collectable():
    """修正后 tests/test_plugin_marketplace_update_reload_ui.py 不再有 collection ImportError

    修复前该模块 `from app.core import plugin_manager as pm_mod` 触发 pytest collection 失败
    （Pre-existing ERROR）。本测试不直接 import（避免触发 cards.py 真实 Qt 依赖链），
    只做语法级断言：源文件不含已废弃的旧 import 路径。
    """
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "test_plugin_marketplace_update_reload_ui.py"
    content = src.read_text(encoding="utf-8")
    assert "from app.core import plugin_manager" not in content, (
        "tests/test_plugin_marketplace_update_reload_ui.py 必须改用 app.plugins.managers.plugin_manager"
    )