# -*- coding: utf-8 -*-
"""plugin-marketplace 工具组件回归（零网络、零磁盘写入）

覆盖：
- manifest 声明 tools 组件与 module_prefixes（AST 安全网放行所必需）
- 7 个工具注册成功、danger 分级正确、schema name 与注册名一致
- 保护分支：插件市场自身不可操作、系统/内置插件不可卸载
- 已装列表分组输出（用户插件详列 / 内置系统折叠）
- 详情工具：输出完整信息/本地目录，未知插件报错
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN = ROOT / "plugins" / "plugin-marketplace"
TOOL_FILE = PLUGIN / "tools" / "marketplace_tools.py"

_TOOL_NAMES = {
    "marketplace_search",
    "marketplace_installed",
    "marketplace_detail",
    "marketplace_install",
    "marketplace_uninstall",
    "marketplace_update",
    "marketplace_set_enabled",
}
_DANGEROUS = {
    "marketplace_install",
    "marketplace_uninstall",
    "marketplace_update",
    "marketplace_set_enabled",
}


@pytest.fixture(scope="module")
def tool_mod():
    from app.plugins.loaders.plugin_tool_loader import _load_module

    mod = _load_module("plugin-marketplace", TOOL_FILE, "system")
    assert mod is not None, "工具模块加载失败（AST guard / 语法）"
    return mod


class _FakeInstaller:
    """只读桩：不扫盘、不联网"""
    last_error = ""

    def __init__(self, status_map, inst_map=None):
        self._status_map = status_map
        self._inst_map = inst_map or {}

    def get_status_map(self):
        return dict(self._status_map)

    def get_installed_map(self):
        return dict(self._inst_map)

    def get_installed_version(self, name):
        return self._inst_map.get(name)

    def get_manifest_map(self):
        return {}


def test_manifest_declares_tools_and_module_prefix():
    data = json.loads((PLUGIN / ".drifox-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert data["components"].get("tools") is True
    # AST 安全网按声明放行工具模块内的私有包注册，缺声明会被拒载
    assert "_plugin_marketplace_ui" in data.get("module_prefixes", [])


def test_registers_seven_tools(tool_mod):
    from app.plugins.loaders.plugin_tool_loader import _PluginRegistryProxy
    from app.tools.registry import ToolRegistry

    registry = ToolRegistry()
    tool_mod.register(_PluginRegistryProxy(registry, "plugin-marketplace", ROOT / "plugins"))
    tools = registry._tools
    assert set(tools) == _TOOL_NAMES
    for name, reg in tools.items():
        fn = reg.schema["function"]
        assert fn["name"] == name
        assert fn["parameters"]["type"] == "object"
        assert reg.danger == ("dangerous" if name in _DANGEROUS else "safe")
        assert reg.source.startswith("plugin:")


def test_rejects_self_operation(tool_mod):
    res = tool_mod._set_enabled_impl({}, plugin="plugin-marketplace", enabled=False)
    assert res.success is False
    assert "自身" in res.error


def test_rejects_system_uninstall(tool_mod, monkeypatch):
    monkeypatch.setattr(
        tool_mod, "_installer", lambda: _FakeInstaller({"system-tools": "system"})
    )
    res = tool_mod._uninstall_impl({}, plugin="system-tools")
    assert res.success is False
    assert "不能卸载" in res.error


def test_install_short_circuits_when_installed(tool_mod, monkeypatch):
    monkeypatch.setattr(
        tool_mod,
        "_installer",
        lambda: _FakeInstaller({"browser": "enabled"}, {"browser": "1.4.4"}),
    )
    res = tool_mod._install_impl({}, plugin="browser")
    assert res.success is True
    assert "已安装" in res.content and "未重复安装" in res.content


def test_installed_list_folds_builtin(tool_mod, monkeypatch):
    monkeypatch.setattr(
        tool_mod,
        "_installer",
        lambda: _FakeInstaller(
            {"demo": "enabled", "system-tools": "system"}, {"demo": "1.0.0", "system-tools": "1.0.0"}
        ),
    )
    res = tool_mod._installed_impl({}, check_update=False)
    assert res.success is True
    assert "1. demo | 已安装·启用 v1.0.0" in res.content
    assert "另有 1 个内置/系统插件" in res.content


def test_detail_reports_unknown_plugin(tool_mod, monkeypatch):
    monkeypatch.setattr(tool_mod, "_installer", lambda: _FakeInstaller({}))
    monkeypatch.setattr(tool_mod, "_find_market_plugin", lambda name, force=False: None)
    res = tool_mod._detail_impl({}, plugin="no-such-plugin")
    assert res.success is False
    assert "没有任何名为" in res.error or "都没有名为" in res.error


def test_detail_outputs_fields_and_local_dir(tool_mod, monkeypatch, tmp_path):
    plugin_dir = tmp_path / "demo"
    (plugin_dir / ".drifox-plugin").mkdir(parents=True)
    (plugin_dir / ".drifox-plugin" / "plugin.json").write_text(
        json.dumps({"name": "demo", "version": "1.2.3"}), encoding="utf-8"
    )

    class _Inst(_FakeInstaller):
        _plugins_dir = tmp_path
        _disabled_dir = tmp_path / "nope"
        _system_dir = tmp_path / "nope2"

        def get_manifest_map(self):
            return {"demo": {"name": "demo", "version": "1.2.3", "components": {"ui": True}}}

    monkeypatch.setattr(
        tool_mod,
        "_installer",
        lambda: _Inst({"demo": "enabled"}, {"demo": "1.2.3"}),
    )
    monkeypatch.setattr(
        tool_mod,
        "_find_market_plugin",
        lambda name, force=False: {
            "name": "demo",
            "version": "1.2.3",
            "description": "演示插件",
            "min_host_version": "0.1.0",
        },
    )
    res = tool_mod._detail_impl({}, plugin="demo")
    assert res.success is True
    assert "插件 demo v1.2.3" in res.content
    assert "描述：演示插件" in res.content
    assert "兼容：平台与宿主版本均满足" in res.content
    assert "组件：ui" in res.content
    assert f"本地目录：{plugin_dir.resolve()}" in res.content
