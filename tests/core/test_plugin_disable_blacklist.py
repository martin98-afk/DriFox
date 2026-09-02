# -*- coding: utf-8 -*-
"""禁用保护名单制回归测试

旧行为：manifest type == "system" 一刀切拒绝禁用，导致 shortcut-manager /
agent_trace / assistant_hub / welcome_changelog 等内置功能增强型插件无法禁用。

新行为：黑名单制——仅 PluginManager._NON_DISABLEABLE（system /
plugin-marketplace）拒绝禁用（禁用会断核心链路且无法恢复），
其余插件均可禁用；type 字段回归纯元数据语义。
"""

from pathlib import Path

from app.plugins.managers.plugin_manager import PluginManager

_TEST_PLUGIN = "test-disable-list"


def test_blacklist_rejects_core_plugins(tmp_path, monkeypatch):
    """黑名单插件（system/plugin-marketplace）拒绝禁用，无论 manifest type。"""
    from app.plugins.managers.plugin_manager import PluginManager

    assert "system" in PluginManager._NON_DISABLEABLE
    assert "plugin-marketplace" in PluginManager._NON_DISABLEABLE


def test_builtin_system_type_plugin_can_be_disabled(tmp_path, monkeypatch):
    """manifest type=system 的内置插件（非黑名单）允许禁用（名单制核心行为）。"""
    pm = PluginManager.__new__(PluginManager)

    def _make_fake_plugin(base: Path, name: str, manifest_type: str) -> Path:
        plugin_dir = base / "plugins" / name
        manifest_dir = plugin_dir / ".drifox-plugin"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "plugin.json").write_text(
            f'{{"name": "{name}", "type": "{manifest_type}"}}', encoding="utf-8"
        )
        return plugin_dir

    plugin_dir = _make_fake_plugin(tmp_path, _TEST_PLUGIN, "system")

    # 构造 PluginInfo 假对象：manifest type=system 但名字不在黑名单
    class _Info:
        name = _TEST_PLUGIN
        path = plugin_dir
        manifest = {"name": _TEST_PLUGIN, "type": "system"}

    info = _Info()
    monkeypatch.setattr(pm, "_plugins", {_TEST_PLUGIN: info}, raising=False)
    monkeypatch.setattr(pm, "_get_enabled_set", lambda: {_TEST_PLUGIN})
    monkeypatch.setattr(pm, "_get_disabled_set", lambda: set())
    saved = {}
    monkeypatch.setattr(pm, "_save_enabled_set", lambda s: saved.__setitem__("enabled", set(s)))
    monkeypatch.setattr(pm, "_save_disabled_set", lambda s: saved.__setitem__("disabled", set(s)))
    monkeypatch.setattr(pm, "invalidate_mcp_cache", lambda: None)
    monkeypatch.setattr(pm, "_unload_plugin_ui", lambda n: None)
    monkeypatch.setattr(pm, "_rescan_plugin_tools", lambda n, enabled: None)
    monkeypatch.setattr(pm, "_trigger_plugin_changed_hook", lambda *a, **k: None)

    pm.disable_plugin(_TEST_PLUGIN)

    assert saved["disabled"] == {_TEST_PLUGIN}, "type=system 的非黑名单插件应写入禁用集"
    assert saved["enabled"] == set()
