# -*- coding: utf-8 -*-
"""安全模式（--safe-mode）加载隔离测试。

契约：安全模式只加载系统插件。发现缓存不可读也不可写（缓存 key 只含磁盘
目录签名，读写会让正常启动命中「仅 system」缓存）；热重载、组件 loader、
user-custom 的兜底目录（命令/智能体/LSP）都不得把用户侧内容带回来。
"""

import sys
from pathlib import Path

import pytest


def _make_pm(tmp_path: Path, safe_mode: bool = True):
    """最小化 PluginManager 实例（不走单例 __init__）。"""
    from app.plugins.managers.plugin_manager import PluginManager

    pm = PluginManager.__new__(PluginManager)
    pm._initialized = False
    pm._plugins = {}
    pm._app_data_dir = tmp_path
    pm._safe_mode = safe_mode
    pm._pending_config_cards = {}
    pm._disabled_components_cache = None
    pm._last_scan_signature = None
    pm._SYSTEM_PLUGIN_DIR = tmp_path / "system-plugins"
    pm._CLAUDE_USER_SKILLS_DIR = tmp_path / "claude-skills"
    pm._CLAUDE_PLUGIN_CACHE_DIR = tmp_path / "claude-cache"
    pm._USER_PLUGIN_DIR_NAME = "plugins"
    return pm


def test_is_safe_mode_reads_argv(monkeypatch):
    from app.utils.safe_mode import is_safe_mode

    monkeypatch.setattr(sys, "argv", ["main.py"])
    assert is_safe_mode() is False
    monkeypatch.setattr(sys, "argv", ["main.py", "--safe-mode"])
    assert is_safe_mode() is True


def test_initialize_safe_mode_skips_cache_and_user_dirs(monkeypatch, tmp_path):
    """安全模式初始化：不读缓存、不扫 Claude/用户插件、不回写缓存。"""
    pm = _make_pm(tmp_path, True)
    called: list[str] = []
    monkeypatch.setattr(pm, "_migrate_split_system_states", lambda: called.append("migrate"))
    monkeypatch.setattr(pm, "_try_load_from_discovery_cache", lambda: called.append("cache") or True)
    monkeypatch.setattr(pm, "_discover_system_plugins", lambda: called.append("system"))
    monkeypatch.setattr(pm, "_discover_claude_plugins", lambda: called.append("claude"))
    monkeypatch.setattr(pm, "_discover_user_plugins", lambda d: called.append("user"))
    monkeypatch.setattr(pm, "_restore_enabled_from_settings", lambda: None)
    monkeypatch.setattr(pm, "_plugins_dir_signature", lambda include_mtime=True: ("sig",))
    monkeypatch.setattr(pm, "_save_discovery_cache", lambda: called.append("save"))
    monkeypatch.setattr(pm, "_schedule_preload_plugin_tools", lambda: None)

    pm.initialize()

    assert pm.is_initialized() is True
    assert pm.is_safe_mode() is True
    assert called == ["migrate", "system"]


def test_initialize_normal_mode_still_uses_cache(monkeypatch, tmp_path):
    """对照：正常模式仍走缓存命中路径（确认门禁只在安全模式生效）。"""
    pm = _make_pm(tmp_path, False)
    called: list[str] = []
    monkeypatch.setattr(pm, "_migrate_split_system_states", lambda: None)
    monkeypatch.setattr(pm, "_try_load_from_discovery_cache", lambda: called.append("cache") or True)
    monkeypatch.setattr(pm, "_restore_enabled_from_settings", lambda: None)
    monkeypatch.setattr(pm, "_backfill_scan_signature", lambda: None)
    monkeypatch.setattr(pm, "_schedule_preload_plugin_tools", lambda: None)

    pm.initialize()

    assert called == ["cache"]


def test_safe_mode_does_not_save_discovery_cache(monkeypatch, tmp_path):
    """安全模式不落盘发现缓存，避免正常启动命中「无用户插件」缓存。"""
    pm = _make_pm(tmp_path, True)
    pm._plugins = {"system-core": object()}
    cache_file = tmp_path / "cache" / "plugin_discovery.json"
    monkeypatch.setattr(pm, "_discovery_cache_path", lambda: cache_file)

    pm._save_discovery_cache()

    assert not cache_file.exists()


def test_safe_mode_rescan_only_scans_system(monkeypatch, tmp_path):
    """运行期热重扫：安全模式只扫系统目录。"""
    pm = _make_pm(tmp_path, True)
    pm._initialized = True
    pm._plugins = {"system-core": object()}
    pm._last_scan_signature = ("old",)
    scanned: list[str] = []

    def fake_scan(base_dir, plugin_type):
        scanned.append(plugin_type)
        return []

    monkeypatch.setattr(pm, "_scan_plugins", fake_scan)
    monkeypatch.setattr(pm, "_plugins_dir_signature", lambda include_mtime=True: ("new",))
    monkeypatch.setattr(pm, "_merge_discovered", lambda s, c, u, allow: ({}, []))
    monkeypatch.setattr(pm, "_apply_deps_conflict_gate", lambda plugins: None)
    monkeypatch.setattr(pm, "_unload_plugin_ui", lambda name: None)
    monkeypatch.setattr(pm, "_unregister_config_schema", lambda name: None)
    monkeypatch.setattr(pm, "_save_discovery_cache", lambda: None)

    pm.rescan()

    assert scanned == ["system"]


def test_safe_mode_discover_and_register_skips_user_dirs(monkeypatch, tmp_path):
    """热更新发现新插件：安全模式不搜 Claude/用户目录。"""
    pm = _make_pm(tmp_path, True)
    searched: list[tuple[str, str]] = []
    monkeypatch.setattr(
        pm, "_scan_one_plugin_dir", lambda d, t: searched.append((str(d), t)) or None
    )

    # 同名插件只存在于用户目录，系统目录不存在该插件
    user_plugin = tmp_path / "plugins" / "foo"
    user_plugin.mkdir(parents=True)

    assert pm._discover_and_register("foo") is None
    assert searched == []


def test_plugin_roots_excludes_user_root_in_safe_mode(monkeypatch):
    """组件 loader 的插件根：安全模式只返回系统根。"""
    import app.plugins.loaders._roots as roots_mod

    monkeypatch.setattr(roots_mod, "is_safe_mode", lambda: True)
    roots = roots_mod._plugin_roots()
    assert len(roots) == 1
    assert roots[0].name == "plugins"

    monkeypatch.setattr(roots_mod, "is_safe_mode", lambda: False)
    assert len(roots_mod._plugin_roots()) >= 1


def test_safe_mode_command_files_skip_user_custom(monkeypatch, tmp_path):
    """命令加载：安全模式跳过 user-custom 兜底目录。"""
    pm = _make_pm(tmp_path, True)
    user_cmd = tmp_path / "plugins" / "user-custom" / "commands"
    user_cmd.mkdir(parents=True)
    (user_cmd / "custom.md").write_text("# custom", encoding="utf-8")

    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(pm, "_iter_enabled_plugins", lambda component="": iter(()))

    assert pm.get_command_files() == []
    assert pm.get_command_files_for_plugin("user-custom") == []


def test_safe_mode_agent_files_skip_user_custom(monkeypatch, tmp_path):
    """智能体加载：安全模式跳过 user-custom 兜底目录。"""
    pm = _make_pm(tmp_path, True)
    user_agents = tmp_path / "plugins" / "user-custom" / "agents"
    user_agents.mkdir(parents=True)
    (user_agents / "bad.md").write_text("# bad", encoding="utf-8")

    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(pm, "_iter_enabled_plugins", lambda component="": iter(()))

    assert pm.get_agent_files() == []


def test_safe_mode_lsp_configs_skip_manifestless_user_dir(monkeypatch, tmp_path):
    """LSP 配置：安全模式不扫描用户目录下无 manifest 的 .lsp.json。"""
    pm = _make_pm(tmp_path, True)
    broken = tmp_path / "plugins" / "broken"
    broken.mkdir(parents=True)
    (broken / ".lsp.json").write_text('{"py": {"command": "pyright"}}', encoding="utf-8")

    monkeypatch.setattr(pm, "_iter_enabled_plugins", lambda component="": iter(()))
    monkeypatch.setattr(pm, "_get_disabled_components", lambda: set())

    assert pm.get_lsp_configs() == []
