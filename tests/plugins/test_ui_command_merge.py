# -*- coding: utf-8 -*-
"""UI 入口命令与插件自带 md 命令合并的回归测试

背景：UI 插件同时声明 components.commands 与 components.ui 且 id 与 md 同名时，
旧行为会注册出两条命令（`/quick-screenshot` 与 `/quick-screenshot:quick-screenshot`），
在快捷键管理器里显示为两行、都可绑键；绑成同一按键后 Qt 判 ambiguous，
两条都不触发且无任何报错（2026-10 用户报告）。

本测试锁定：
1. 同名命令的 source 是本插件自己的 md → 复用短名，不产生 namespaced 双行
2. 同名命令来自其它插件/内置 → 仍加前缀（保持既有让位语义）
3. 旧版 namespaced 键一次性迁移到短名并清理
4. 快捷键冲突汇总（供列表标红）
"""

import json
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_app_data(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    import app.utils.utils as U

    monkeypatch.setattr(U, "get_app_data_dir", lambda: tmp)
    from app.plugins.registries import ui_plugin_registry as R

    R._shortcuts_cache = None
    yield tmp
    R._shortcuts_cache = None


@pytest.fixture()
def clean_cmd_manager():
    from app.core.commands.command_manager import CommandManager

    mgr = CommandManager.get_instance()
    saved = dict(mgr._commands)
    yield mgr
    mgr._commands = saved


@pytest.fixture()
def clean_registry():
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    reg = UIPluginRegistry.get_instance()
    reg.reset()
    yield reg
    reg.reset()


def test_same_source_md_reuses_short_name(isolated_app_data, clean_cmd_manager, clean_registry):
    """本插件自己的 md 命令已占用短名 → UI 入口仍复用短名（合并成一条）"""
    from app.core.commands.command_manager import CommandType

    clean_cmd_manager.register(
        name="quick-screenshot",
        command_type=CommandType.FUNCTION,
        description="一键选区截图",
        shortcut="",
        source="plugin:quick-screenshot",
    )

    reg = clean_registry
    assert reg._ui_command_name("quick-screenshot", "quick-screenshot") == "quick-screenshot"


def test_foreign_command_still_namespaced(isolated_app_data, clean_cmd_manager, clean_registry):
    """短名被其它来源占用 → 仍加插件前缀（不抢占他人命令）"""
    from app.core.commands.command_manager import CommandType

    clean_cmd_manager.register(
        name="shared-id",
        command_type=CommandType.FUNCTION,
        description="别的来源",
        source="plugin:other-plugin",
    )

    reg = clean_registry
    assert reg._ui_command_name("shared-id", "my-plugin") == "my-plugin:shared-id"


def test_builtin_command_still_namespaced(isolated_app_data, clean_cmd_manager, clean_registry):
    """内置命令（source 为空）同名 → 仍加前缀"""
    from app.core.commands.command_manager import CommandType

    clean_cmd_manager.register(
        name="history",
        command_type=CommandType.FUNCTION,
        description="内置历史",
        source="",
    )

    reg = clean_registry
    assert reg._ui_command_name("history", "my-plugin") == "my-plugin:history"


def test_legacy_shortcut_key_migrated(isolated_app_data, clean_cmd_manager, clean_registry):
    """旧版 <plugin>:<base> 键在合并后一次性迁移到短名，旧键删除"""
    from app.plugins.registries import ui_plugin_registry as R

    R.save_ui_command_shortcut("quick-screenshot:quick-screenshot", "Ctrl+Shift+S")

    reg = clean_registry
    got = reg._resolve_ui_command_shortcut("quick-screenshot", "quick-screenshot", clean_cmd_manager)

    assert got == "Ctrl+Shift+S"
    mapping = R.load_ui_command_shortcuts()
    assert mapping.get("quick-screenshot") == "Ctrl+Shift+S"
    assert "quick-screenshot:quick-screenshot" not in mapping


def test_json_shortcut_wins_over_md(isolated_app_data, clean_cmd_manager, clean_registry):
    """shortcuts.json 优先级高于 md frontmatter"""
    from app.core.commands.command_manager import CommandType
    from app.plugins.registries import ui_plugin_registry as R

    clean_cmd_manager.register(
        name="cmd-a",
        command_type=CommandType.FUNCTION,
        description="md 里的",
        shortcut="Ctrl+1",
        source="plugin:plug",
    )
    R.save_ui_command_shortcut("cmd-a", "Ctrl+2")

    reg = clean_registry
    assert reg._resolve_ui_command_shortcut("cmd-a", "plug", clean_cmd_manager) == "Ctrl+2"


def test_md_shortcut_used_when_json_empty(isolated_app_data, clean_cmd_manager, clean_registry):
    """JSON 无映射时沿用同源 md 的快捷键（合并后不丢用户旧配置）"""
    from app.core.commands.command_manager import CommandType

    clean_cmd_manager.register(
        name="cmd-b",
        command_type=CommandType.FUNCTION,
        description="md 里的",
        shortcut="Ctrl+3",
        source="plugin:plug",
    )

    reg = clean_registry
    assert reg._resolve_ui_command_shortcut("cmd-b", "plug", clean_cmd_manager) == "Ctrl+3"


def test_no_shortcut_for_foreign_md(isolated_app_data, clean_cmd_manager, clean_registry):
    """其它插件的 md 快捷键不被借用"""
    from app.core.commands.command_manager import CommandType

    clean_cmd_manager.register(
        name="cmd-c",
        command_type=CommandType.FUNCTION,
        description="别人的",
        shortcut="Ctrl+4",
        source="plugin:other",
    )

    reg = clean_registry
    assert reg._resolve_ui_command_shortcut("cmd-c", "plug", clean_cmd_manager) == ""


def test_shortcuts_json_isolated_file(isolated_app_data):
    """迁移写入落在隔离的数据目录内（不污染真实用户配置）"""
    from app.plugins.registries import ui_plugin_registry as R

    R.save_ui_command_shortcut("x:y", "Ctrl+5")
    f = isolated_app_data / "plugins/user-custom/shortcuts.json"
    assert json.loads(f.read_text(encoding="utf-8")) == {"x:y": "Ctrl+5"}
