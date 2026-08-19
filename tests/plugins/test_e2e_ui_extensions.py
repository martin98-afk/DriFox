# -*- coding: utf-8 -*-
"""Phase D E2E：临时插件文件经 load_plugin 注册四类扩展点 → 卸载全部清理。

验证链路（与真实插件加载一致）：
插件目录/ui/__init__.py → register_ui(registry) → 四类注册生效 → unload_plugin 全清。
存量兼容：floating card 类插件（已有扩展点）不受影响。
"""

from pathlib import Path

import pytest

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

_PLUGIN_CODE = '''
# -*- coding: utf-8 -*-
"""E2E 验收插件：注册四类 Phase D 扩展点"""
from PyQt5.QtWidgets import QWidget


class DemoSettingsCard(QWidget):
    """插件设置卡片（真实 QWidget 子类）"""
    pass


def register_ui(registry):
    # 1. 侧边栏项
    registry.register_sidebar_item(
        "demo-plugin", "demo-sidebar", "演示侧边栏", group="custom",
        on_click=lambda ctx: None,
    )
    # 2. 输入区按钮
    registry.register_input_button(
        "demo-plugin", "demo-button", tooltip="演示按钮", on_click=lambda ctx: None,
    )
    # 3. 右键菜单项（message_card + tab 各一）
    registry.register_context_menu_action(
        "demo-plugin", "demo-menu-msg", target="message_card", label="演示菜单",
        action_func=lambda ctx: True,
    )
    registry.register_context_menu_action(
        "demo-plugin", "demo-menu-tab", target="tab", label="演示Tab菜单",
        action_func=lambda ctx: True,
    )
    # 4. 设置卡片
    registry.register_settings_card("demo-plugin", "demo-settings", "演示设置卡", DemoSettingsCard)
'''


@pytest.fixture()
def fresh_registry(monkeypatch, tmp_path):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


def _write_plugin(tmp_path: Path, name: str = "demo-plugin") -> Path:
    plugin_dir = tmp_path / name
    ui_dir = plugin_dir / "ui"
    ui_dir.mkdir(parents=True)
    (ui_dir / "__init__.py").write_text(_PLUGIN_CODE, encoding="utf-8")
    return plugin_dir


def test_plugin_load_registers_all_four(fresh_registry, tmp_path):
    """load_plugin → 四类扩展点全部注册生效"""
    plugin_dir = _write_plugin(tmp_path)
    assert fresh_registry.load_plugin("demo-plugin", plugin_dir) is True

    assert [i.item_id for i in fresh_registry.get_sidebar_items()] == ["demo-sidebar"]
    assert [b.button_id for b in fresh_registry.get_input_buttons()] == ["demo-button"]
    assert [a.action_id for a in fresh_registry.get_context_actions("message_card")] == ["demo-menu-msg"]
    assert [a.action_id for a in fresh_registry.get_context_actions("tab")] == ["demo-menu-tab"]
    assert [c.card_id for c in fresh_registry.get_settings_cards()] == ["demo-settings"]
    assert fresh_registry.is_loaded("demo-plugin")


def test_plugin_unload_clears_all(fresh_registry, tmp_path):
    """unload_plugin → 四类扩展点全部清理（热重载/卸载路径）"""
    plugin_dir = _write_plugin(tmp_path)
    fresh_registry.load_plugin("demo-plugin", plugin_dir)

    assert fresh_registry.unload_plugin("demo-plugin") is True
    assert fresh_registry.get_sidebar_items() == []
    assert fresh_registry.get_input_buttons() == []
    assert fresh_registry.get_context_actions("message_card") == []
    assert fresh_registry.get_context_actions("tab") == []
    assert fresh_registry.get_settings_cards() == []
    assert not fresh_registry.is_loaded("demo-plugin")


def test_plugin_reload_updates_registration(fresh_registry, tmp_path):
    """热重载：修改插件代码 → load_plugin 重新加载 → 新注册生效（旧注册被清）"""
    plugin_dir = _write_plugin(tmp_path)
    fresh_registry.load_plugin("demo-plugin", plugin_dir)

    # 修改插件：侧边栏项改名
    new_code = _PLUGIN_CODE.replace('"demo-sidebar", "演示侧边栏"', '"demo-sidebar", "改名侧边栏"')
    (plugin_dir / "ui" / "__init__.py").write_text(new_code, encoding="utf-8")
    assert fresh_registry.load_plugin("demo-plugin", plugin_dir) is True

    items = fresh_registry.get_sidebar_items()
    assert len(items) == 1 and items[0].label == "改名侧边栏"


def test_legacy_plugins_unaffected(fresh_registry, tmp_path):
    """存量 floating card 插件（已有扩展点）注册不受新扩展点影响"""
    plugin_dir = _write_plugin(tmp_path)
    # 模拟存量插件：仅注册 floating card
    legacy_dir = tmp_path / "legacy-plugin"
    (legacy_dir / "ui").mkdir(parents=True)
    (legacy_dir / "ui" / "__init__.py").write_text(
        '''
# -*- coding: utf-8 -*-
from PyQt5.QtWidgets import QWidget


class LegacyCard(QWidget):
    pass


def register_ui(registry):
    registry.register_floating_card("legacy-plugin", "legacy-card", LegacyCard, container="top", title="存量卡")
''',
        encoding="utf-8",
    )
    assert fresh_registry.load_plugin("demo-plugin", plugin_dir) is True
    assert fresh_registry.load_plugin("legacy-plugin", legacy_dir) is True

    # 四类新扩展点 + 存量 floating card 共存
    assert fresh_registry.get_sidebar_items()
    assert fresh_registry.get_input_buttons()
    assert "legacy-card" in fresh_registry.get_floating_cards()
    # 卸载 demo → 存量插件不受影响
    fresh_registry.unload_plugin("demo-plugin")
    assert "legacy-card" in fresh_registry.get_floating_cards()
    assert fresh_registry.get_sidebar_items() == []


def test_bad_plugin_skipped(fresh_registry, tmp_path):
    """无 register_ui 的插件目录 → load_plugin 返回 False 不影响其他"""
    bad_dir = tmp_path / "bad-plugin"
    (bad_dir / "ui").mkdir(parents=True)
    (bad_dir / "ui" / "__init__.py").write_text("# no register_ui", encoding="utf-8")
    assert fresh_registry.load_plugin("bad-plugin", bad_dir) is False
    assert not fresh_registry.is_loaded("bad-plugin")


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
