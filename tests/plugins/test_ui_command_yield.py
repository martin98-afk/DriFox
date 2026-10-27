# -*- coding: utf-8 -*-
"""UI 入口与插件 md 命令合并后的注销语义

让位（override_external=False）且同源（source=plugin:<owner>）时，UI 命令不接管
命令本体，只在 _ui_command_names 里打 UI 标记。卸载 UI 组件时必须摘掉该标记，
否则命令表里那条 md 命令会被继续当成 UI 命令：

- 快捷键被写进 shortcuts.json 而不是 md frontmatter
- 用户重装插件后两份配置打架，且 md 改了 shortcut 也不生效
"""

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


def test_yield_same_source_marks_ui_name(isolated_app_data, clean_cmd_manager, clean_registry):
    """让位同源时命令名进 _ui_command_names（列表按 UI 命令展示）"""
    from app.core.commands.command_manager import CommandType

    clean_cmd_manager.register(
        name="dup-id",
        command_type=CommandType.FUNCTION,
        description="md 的",
        source="plugin:plug",
    )
    reg = clean_registry
    reg.register_ui_command(
        name="dup-id",
        description="UI 的",
        handler=lambda a: None,
        owner="plug",
        override_external=False,
    )
    assert "dup-id" in reg.get_ui_command_names()


def test_unregister_clears_ui_name_for_yielded_command(isolated_app_data, clean_cmd_manager, clean_registry):
    """卸载 UI 组件后摘掉 UI 标记，但命令本体（md 注册的）必须保留"""
    from app.core.commands.command_manager import CommandType

    clean_cmd_manager.register(
        name="dup-id",
        command_type=CommandType.FUNCTION,
        description="md 的",
        source="plugin:plug",
    )
    reg = clean_registry
    reg.register_ui_command(
        name="dup-id",
        description="UI 的",
        handler=lambda a: None,
        owner="plug",
        override_external=False,
    )
    reg.unregister_ui_command("dup-id")

    assert "dup-id" not in reg.get_ui_command_names()
    # 命令本体仍在（由 commands 组件维持）
    assert clean_cmd_manager.has_command("dup-id")


def test_unregister_keeps_foreign_command_untouched(isolated_app_data, clean_cmd_manager, clean_registry):
    """让位给别的插件/内置命令时，注销不得删对方命令"""
    from app.core.commands.command_manager import CommandType

    clean_cmd_manager.register(
        name="ext-id",
        command_type=CommandType.FUNCTION,
        description="别人的",
        source="plugin:other",
    )
    reg = clean_registry
    reg.register_ui_command(
        name="ext-id",
        description="UI 的",
        handler=lambda a: None,
        owner="plug",
        override_external=False,
    )
    reg.unregister_ui_command("ext-id")

    assert clean_cmd_manager.has_command("ext-id")
    assert "ext-id" not in reg.get_ui_command_names()
