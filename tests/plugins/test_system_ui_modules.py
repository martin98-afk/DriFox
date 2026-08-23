# -*- coding: utf-8 -*-
"""Phase F：5 个系统 UI 模块注册 + 插件 override"""

import pytest

from app.plugins.contracts.ui_module import UIModule
from app.plugins.registries.ui_plugin_registry import UIPluginRegistry


@pytest.fixture()
def fresh_registry(monkeypatch):
    reg = UIPluginRegistry()
    monkeypatch.setattr(UIPluginRegistry, "_instance", reg)
    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


class TestSystemUIModules:
    def test_chat_area_module(self, fresh_registry):
        from app.widgets.modules.chat_area_module import ChatAreaModule

        fresh_registry.register_ui_module("chat_area", ChatAreaModule, plugin_name="system")
        mod = fresh_registry.get_ui_module("chat_area")
        assert mod is not None
        assert isinstance(mod, UIModule)
        assert mod.module_id == "chat_area"

    def test_title_bar_module(self, fresh_registry):
        from app.widgets.modules.title_bar_module import TitleBarModule

        fresh_registry.register_ui_module("title_bar", TitleBarModule, plugin_name="system")
        mod = fresh_registry.get_ui_module("title_bar")
        assert mod is not None
        assert mod.module_id == "title_bar"

    def test_system_cards_module(self, fresh_registry):
        from app.widgets.modules.system_cards_module import SystemCardsModule

        fresh_registry.register_ui_module("system_cards", SystemCardsModule, plugin_name="system")
        mod = fresh_registry.get_ui_module("system_cards")
        assert mod is not None
        assert mod.module_id == "system_cards"

    def test_input_card_module(self, fresh_registry):
        from app.widgets.modules.input_card_module import InputCardModule

        fresh_registry.register_ui_module("input_card", InputCardModule, plugin_name="system")
        mod = fresh_registry.get_ui_module("input_card")
        assert mod is not None
        assert mod.module_id == "input_card"

    def test_bottom_toolbar_module(self, fresh_registry):
        from app.widgets.modules.bottom_toolbar_module import BottomToolbarModule

        fresh_registry.register_ui_module("bottom_toolbar", BottomToolbarModule, plugin_name="system")
        mod = fresh_registry.get_ui_module("bottom_toolbar")
        assert mod is not None
        assert mod.module_id == "bottom_toolbar"


class TestPluginOverride:
    def test_plugin_can_override_chat_area(self, fresh_registry):
        from app.widgets.modules.chat_area_module import ChatAreaModule

        class _PluginChatArea(UIModule):
            module_id = "chat_area"

            def build(self, host):
                host.overridden = "plugin"

        fresh_registry.register_ui_module("chat_area", ChatAreaModule, plugin_name="system", priority=0)
        fresh_registry.register_ui_module("chat_area", _PluginChatArea, plugin_name="demo", priority=100)
        # 胜者为 plugin
        host = type("_H", (), {})()
        fresh_registry.get_ui_module("chat_area").build(host)
        assert host.overridden == "plugin"

    def test_plugin_can_override_title_bar(self, fresh_registry):
        from app.widgets.modules.title_bar_module import TitleBarModule

        class _PluginTitleBar(UIModule):
            module_id = "title_bar"

            def build(self, host):
                host.overridden = "plugin"

        fresh_registry.register_ui_module("title_bar", TitleBarModule, plugin_name="system", priority=0)
        fresh_registry.register_ui_module("title_bar", _PluginTitleBar, plugin_name="demo", priority=100)
        host = type("_H", (), {})()
        fresh_registry.get_ui_module("title_bar").build(host)
        assert host.overridden == "plugin"

    def test_plugin_can_override_system_cards(self, fresh_registry):
        from app.widgets.modules.system_cards_module import SystemCardsModule

        class _PluginSystemCards(UIModule):
            module_id = "system_cards"

            def build(self, host):
                host.overridden = "plugin"

        fresh_registry.register_ui_module("system_cards", SystemCardsModule, plugin_name="system", priority=0)
        fresh_registry.register_ui_module("system_cards", _PluginSystemCards, plugin_name="demo", priority=100)
        host = type("_H", (), {})()
        fresh_registry.get_ui_module("system_cards").build(host)
        assert host.overridden == "plugin"

    def test_plugin_can_override_input_card(self, fresh_registry):
        from app.widgets.modules.input_card_module import InputCardModule

        class _PluginInputCard(UIModule):
            module_id = "input_card"

            def build(self, host):
                host.overridden = "plugin"

        fresh_registry.register_ui_module("input_card", InputCardModule, plugin_name="system", priority=0)
        fresh_registry.register_ui_module("input_card", _PluginInputCard, plugin_name="demo", priority=100)
        host = type("_H", (), {})()
        fresh_registry.get_ui_module("input_card").build(host)
        assert host.overridden == "plugin"

    def test_plugin_can_override_bottom_toolbar(self, fresh_registry):
        from app.widgets.modules.bottom_toolbar_module import BottomToolbarModule

        class _PluginBottomToolbar(UIModule):
            module_id = "bottom_toolbar"

            def build(self, host):
                host.overridden = "plugin"

        fresh_registry.register_ui_module("bottom_toolbar", BottomToolbarModule, plugin_name="system", priority=0)
        fresh_registry.register_ui_module("bottom_toolbar", _PluginBottomToolbar, plugin_name="demo", priority=100)
        host = type("_H", (), {})()
        fresh_registry.get_ui_module("bottom_toolbar").build(host)
        assert host.overridden == "plugin"
