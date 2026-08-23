# -*- coding: utf-8 -*-
"""Phase D 四槽位与 Region 存储单源化：旧 API 写入 → region 视图可读；
旧 API 与 region API 写同 id 时行为一致"""

import pytest

from app.plugins.registries.ui_plugin_registry import (
    ContextMenuActionInfo,
    InputButtonInfo,
    SettingsCardInfo,
    SidebarItemInfo,
    UIPluginRegistry,
)


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


class TestMigrationDualView:
    def test_sidebar_item_appears_in_region(self, fresh_registry):
        fresh_registry.register_sidebar_item("demo", "s1", "标签一")
        entries = fresh_registry.get_region_entries("sidebar")
        assert len(entries) == 1
        assert isinstance(entries[0].payload, SidebarItemInfo)
        assert entries[0].payload.item_id == "s1"
        # 旧 API 读路径返回 Info 列表
        items = fresh_registry.get_sidebar_items()
        assert len(items) == 1 and items[0].item_id == "s1"

    def test_input_button_appears_in_region(self, fresh_registry):
        fresh_registry.register_input_button("demo", "b1", icon_path="x.png")
        entries = fresh_registry.get_region_entries("toolbar:input")
        assert len(entries) == 1
        assert isinstance(entries[0].payload, InputButtonInfo)
        buttons = fresh_registry.get_input_buttons()
        assert len(buttons) == 1 and buttons[0].button_id == "b1"

    def test_context_menu_appears_in_region(self, fresh_registry):
        fresh_registry.register_context_menu_action(
            "demo", "a1", target="tab", label="Tab操作", action_func=lambda ctx: True
        )
        entries = fresh_registry.get_region_entries("menu:tab")
        assert len(entries) == 1
        assert isinstance(entries[0].payload, ContextMenuActionInfo)
        actions = fresh_registry.get_context_actions("tab")
        assert len(actions) == 1 and actions[0].action_id == "a1"

    def test_settings_card_appears_in_region(self, fresh_registry):
        class _W:
            pass

        fresh_registry.register_settings_card("demo", "c1", "卡一", _W)
        entries = fresh_registry.get_region_entries("settings:plugins")
        assert len(entries) == 1
        assert isinstance(entries[0].payload, SettingsCardInfo)
        cards = fresh_registry.get_settings_cards()
        assert len(cards) == 1 and cards[0].card_id == "c1"

    def test_undeclared_target_raises(self, fresh_registry):
        """register_context_menu_action 对未声明区域应抛 ValueError"""
        with pytest.raises(ValueError, match="undeclared region"):
            fresh_registry.register_context_menu_action(
                "demo", "aX", target="ghost_target", label="X", action_func=lambda ctx: True
            )

    def test_unload_clears_all_phase_d_slots(self, fresh_registry):
        class _W:
            pass

        fresh_registry.register_sidebar_item("demo", "s1", "X")
        fresh_registry.register_input_button("demo", "b1")
        fresh_registry.register_context_menu_action(
            "demo", "a1", target="tab", label="X", action_func=lambda ctx: True
        )
        fresh_registry.register_settings_card("demo", "c1", "X", _W)

        fresh_registry.unload_plugin("demo")

        assert fresh_registry.get_sidebar_items() == []
        assert fresh_registry.get_input_buttons() == []
        assert fresh_registry.get_context_actions("tab") == []
        assert fresh_registry.get_settings_cards() == []
        # region 视图同步为空
        assert fresh_registry.get_region_entries("sidebar") == []
        assert fresh_registry.get_region_entries("toolbar:input") == []
        assert fresh_registry.get_region_entries("menu:tab") == []
        assert fresh_registry.get_region_entries("settings:plugins") == []
