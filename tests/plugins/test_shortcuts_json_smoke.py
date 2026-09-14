# shortcuts.json 持久化 + _apply_ui_command 覆盖语义回归测试
# 守护 UI 插件命令快捷键链路（2026-09-14 修复：带冒号 UI 命令快捷键重启失效+孤儿命令）
import json
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_app_data(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    import app.utils.utils as U

    monkeypatch.setattr(U, "get_app_data_dir", lambda: tmp)
    yield tmp


@pytest.fixture()
def clean_cmd_manager():
    from app.core.command_manager import CommandManager

    mgr = CommandManager.get_instance()
    saved = dict(mgr._commands)
    yield mgr
    mgr._commands = saved


def test_shortcuts_json_write_and_clear(isolated_app_data):
    from app.plugins.registries import ui_plugin_registry as R

    R._shortcuts_cache = None
    R.save_ui_command_shortcut("quick-screenshot:quick-screenshot", "Ctrl+Shift+S")
    f = isolated_app_data / "plugins/user-custom/shortcuts.json"
    assert json.loads(f.read_text(encoding="utf-8")) == {"quick-screenshot:quick-screenshot": "Ctrl+Shift+S"}

    R.save_ui_command_shortcut("quick-screenshot:quick-screenshot", "")
    assert json.loads(f.read_text(encoding="utf-8")) == {}
    R._shortcuts_cache = None


def test_apply_ui_command_overrides_stale_shortcut(isolated_app_data, clean_cmd_manager):
    """旧兜底 md 先注册带过期 shortcut → UI 命令注册时以 JSON 为准覆盖"""
    from app.core.command_manager import CommandType
    from app.plugins.registries import ui_plugin_registry as R

    R._shortcuts_cache = None
    R.save_ui_command_shortcut("qs:cmd", "Ctrl+Shift+S")
    clean_cmd_manager.register(name="qs:cmd", command_type=CommandType.FUNCTION, description="旧", shortcut="F9")

    reg = R.UIPluginRegistry.get_instance()
    reg.register_ui_command(name="qs:cmd", description="新", handler=lambda a: None, owner="tp")
    cmd = clean_cmd_manager.get_command("qs:cmd")
    assert cmd.shortcut == "Ctrl+Shift+S"
    assert cmd.description == "新"

    reg.unregister_ui_command("qs:cmd")
    R._shortcuts_cache = None


def test_apply_ui_command_preserves_external_when_no_mapping(isolated_app_data, clean_cmd_manager):
    """JSON 无映射 + 同名外部命令 → 不抢占（让位语义不受影响）"""
    from app.core.command_manager import CommandType
    from app.plugins.registries import ui_plugin_registry as R

    R._shortcuts_cache = None
    clean_cmd_manager.register(name="ext:cmd", command_type=CommandType.FUNCTION, description="外部", shortcut="F10")

    reg = R.UIPluginRegistry.get_instance()
    reg.register_ui_command(name="ext:cmd", description="UI", handler=lambda a: None, owner="tp", override_external=False)
    assert clean_cmd_manager.get_command("ext:cmd").shortcut == "F10"

    reg.unregister_ui_command("ext:cmd")
    R._shortcuts_cache = None


def test_fresh_ui_command_registers_with_shortcut(isolated_app_data, clean_cmd_manager):
    """新命令 + JSON 映射 → 直接带 shortcut 注册；注销后映射保留（重装恢复）"""
    from app.plugins.registries import ui_plugin_registry as R

    R._shortcuts_cache = None
    R.save_ui_command_shortcut("fresh:cmd", "Ctrl+Alt+K")
    reg = R.UIPluginRegistry.get_instance()
    reg.register_ui_command(name="fresh:cmd", description="新命令", handler=lambda a: None, owner="tp")
    assert clean_cmd_manager.get_command("fresh:cmd").shortcut == "Ctrl+Alt+K"

    reg.unregister_ui_command("fresh:cmd")
    assert not clean_cmd_manager.has_command("fresh:cmd")
    assert R.load_ui_command_shortcuts().get("fresh:cmd") == "Ctrl+Alt+K"  # 重装后自动恢复

    R.save_ui_command_shortcut("fresh:cmd", "")  # 清理
    R._shortcuts_cache = None
