# -*- coding: utf-8 -*-
"""UIPluginRegistry 扩展：SidebarItem / InputButton / ContextMenuAction / SettingsCard
四类 Info 注册 + priority 覆盖 + unregister 清理"""

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


def _noop(**kwargs):
    return True


class TestSidebarItem:
    def test_register_and_get(self, fresh_registry):
        fresh_registry.register_sidebar_item(
            "demo", "item-1", "侧边栏项", icon_path="icon.svg", group="custom", on_click=_noop
        )
        items = fresh_registry.get_sidebar_items()
        assert len(items) == 1
        info = items[0]
        assert isinstance(info, SidebarItemInfo)
        assert info.plugin_name == "demo" and info.item_id == "item-1"
        assert info.group == "custom" and info.default_visible is True

    def test_priority_override(self, fresh_registry):
        """同 item_id 高 priority 覆盖低者；低 priority 注册被忽略"""
        fresh_registry.register_sidebar_item("demo", "item-1", "低优先级", priority=1)
        fresh_registry.register_sidebar_item("demo", "item-1", "高优先级", priority=5)
        items = fresh_registry.get_sidebar_items()
        assert len(items) == 1 and items[0].label == "高优先级"
        # 低优先级再次注册被忽略
        fresh_registry.register_sidebar_item("demo", "item-1", "再次低", priority=1)
        assert fresh_registry.get_sidebar_items()[0].label == "高优先级"

    def test_group_order_system_first(self, fresh_registry):
        """group 排序：system 在前，custom 在后（注册序倒置验证）"""
        fresh_registry.register_sidebar_item("demo", "c1", "自定义", group="custom")
        fresh_registry.register_sidebar_item("demo", "s1", "系统", group="system")
        assert [i.item_id for i in fresh_registry.get_sidebar_items()] == ["s1", "c1"]


class TestInputButton:
    def test_register_and_get(self, fresh_registry):
        fresh_registry.register_input_button(
            "demo", "btn-1", icon_path="icon.svg", tooltip="提示", on_click=_noop
        )
        buttons = fresh_registry.get_input_buttons()
        assert len(buttons) == 1
        info = buttons[0]
        assert isinstance(info, InputButtonInfo)
        assert info.button_id == "btn-1" and info.tooltip == "提示"

    def test_priority_override(self, fresh_registry):
        fresh_registry.register_input_button("demo", "btn-1", tooltip="低", priority=1)
        fresh_registry.register_input_button("demo", "btn-1", tooltip="高", priority=5)
        assert fresh_registry.get_input_buttons()[0].tooltip == "高"


class TestContextMenuAction:
    def test_register_and_get_by_target(self, fresh_registry):
        fresh_registry.register_context_menu_action(
            "demo", "act-1", target="message_card", label="菜单项", action_func=_noop
        )
        fresh_registry.register_context_menu_action(
            "demo", "act-2", target="tab", label="Tab菜单项", action_func=_noop
        )
        msg_actions = fresh_registry.get_context_actions("message_card")
        tab_actions = fresh_registry.get_context_actions("tab")
        assert len(msg_actions) == 1 and msg_actions[0].action_id == "act-1"
        assert len(tab_actions) == 1 and tab_actions[0].action_id == "act-2"
        assert fresh_registry.get_context_actions("nonexistent") == []

    def test_separator_and_enabled(self, fresh_registry):
        fresh_registry.register_context_menu_action(
            "demo", "a1", target="message_card", label="A", action_func=_noop, separator_before=True
        )
        fresh_registry.register_context_menu_action(
            "demo", "a2", target="message_card", label="B", action_func=_noop, enabled_func=lambda ctx: False
        )
        actions = fresh_registry.get_context_actions("message_card")
        assert actions[0].separator_before is True
        assert actions[1].enabled_func({"x": 1}) is False

    def test_priority_override(self, fresh_registry):
        fresh_registry.register_context_menu_action("demo", "a1", target="message_card", label="低", action_func=_noop, priority=1)
        fresh_registry.register_context_menu_action("demo", "a1", target="message_card", label="高", action_func=_noop, priority=5)
        assert fresh_registry.get_context_actions("message_card")[0].label == "高"


class TestSettingsCard:
    def test_register_and_get(self, fresh_registry):
        class _FakeCard:
            pass

        fresh_registry.register_settings_card("demo", "card-1", "插件设置卡", _FakeCard)
        cards = fresh_registry.get_settings_cards()
        assert len(cards) == 1
        info = cards[0]
        assert isinstance(info, SettingsCardInfo)
        assert info.card_id == "card-1" and info.widget_class is _FakeCard

    def test_priority_override(self, fresh_registry):
        class _Card:
            pass

        fresh_registry.register_settings_card("demo", "card-1", "低", _Card, priority=1)
        fresh_registry.register_settings_card("demo", "card-1", "高", _Card, priority=5)
        assert fresh_registry.get_settings_cards()[0].title == "高"


class TestUnregisterCleanup:
    def test_unload_plugin_clears_all_four(self, fresh_registry):
        """unload_plugin 按 plugin_name 清理四类扩展点注册"""
        class _Card:
            pass

        fresh_registry._loaded_plugins.add("demo")
        fresh_registry.register_sidebar_item("demo", "item-1", "项")
        fresh_registry.register_input_button("demo", "btn-1", tooltip="按钮")
        fresh_registry.register_context_menu_action("demo", "act-1", target="message_card", label="菜单", action_func=_noop)
        fresh_registry.register_settings_card("demo", "card-1", "卡", _Card)
        fresh_registry.register_sidebar_item("other", "item-2", "他项")

        assert fresh_registry.unload_plugin("demo") is True
        # demo 的四类注册被清理
        assert fresh_registry.get_input_buttons() == []
        assert fresh_registry.get_context_actions("message_card") == []
        assert fresh_registry.get_settings_cards() == []
        # 其他插件注册不受影响（other 的 item-2 保留）
        assert [i.item_id for i in fresh_registry.get_sidebar_items()] == ["item-2"]

    def test_unload_plugin_idempotent(self, fresh_registry):
        """重复卸载不炸（热重载幂等）"""
        fresh_registry._loaded_plugins.add("demo")
        fresh_registry.register_input_button("demo", "btn-1", tooltip="按钮")
        assert fresh_registry.unload_plugin("demo") is True
        assert fresh_registry.unload_plugin("demo") is False  # 已卸载

    def test_unload_plugin_clears_config_schema_autocard(self, fresh_registry):
        """回归：无 ui/ 组件但注册了 config_schema 自动设置卡的插件（如 gateway
        平台插件）卸载时其 settings card 必须被清理，否则残留空卡片「插件配置」。

        此类插件从未走 load_plugin，不在 _loaded_plugins，旧逻辑在此早退零清理。
        """
        class _Card:
            pass

        # 仅注册 settings card，不加入 _loaded_plugins（模拟无 ui/ 组件的插件）
        fresh_registry.register_settings_card("gw-feishu", "gw-feishu-config", "飞书配置", _Card)
        assert "gw-feishu" not in fresh_registry._loaded_plugins

        # 卸载（插件市场卸载流程最终走此路径）
        assert fresh_registry.unload_plugin("gw-feishu") is True
        assert fresh_registry.get_settings_cards() == []

        # 幂等：二次卸载无残留，返回 False
        assert fresh_registry.unload_plugin("gw-feishu") is False


class TestHideFloatingCardGlobally:
    """EP6 公开 API：插件可经此方法隐藏浮动卡片（无需触碰 _card_manager/_window_id）。"""

    def _register_card(self, fresh_registry, card_id="autoloop-config"):
        class _FakeCard:
            pass

        fresh_registry.register_floating_card(
            plugin_name="autoloop",
            card_id=card_id,
            widget_class=_FakeCard,
            container="full",
            title="AutoLoop 配置",
        )

    def test_returns_false_when_no_host(self, fresh_registry, monkeypatch):
        """TabManagerWindow 不可用时返回 False，由调用方回退"""
        self._register_card(fresh_registry)
        # 强制 _resolve_global_host 返回 None
        monkeypatch.setattr(fresh_registry, "_resolve_global_host", lambda: None)
        assert fresh_registry.hide_floating_card_globally("autoloop-config") is False

    def test_returns_false_for_unregistered_card(self, fresh_registry, monkeypatch):
        """未注册的 card_id 不报错，返回 False（保护性 API）"""
        fake_host = type("Host", (), {"_card_manager": _FakeCardManager(), "_window_id": "global"})()
        monkeypatch.setattr(fresh_registry, "_resolve_global_host", lambda: fake_host)
        assert fresh_registry.hide_floating_card_globally("not-registered") is False

    def test_calls_card_manager_hide_when_host_available(self, fresh_registry, monkeypatch):
        """Tab host 可用 + 卡片已注册 → 走 CardManager.hide_card(card_id, host_wid)"""
        self._register_card(fresh_registry)
        cm = _FakeCardManager()
        fake_host = type("Host", (), {"_card_manager": cm, "_window_id": "global-tab"})()
        monkeypatch.setattr(fresh_registry, "_resolve_global_host", lambda: fake_host)
        assert fresh_registry.hide_floating_card_globally("autoloop-config") is True
        assert cm.calls == [("autoloop-config", "global-tab")]

    def test_returns_false_when_card_manager_missing(self, fresh_registry, monkeypatch):
        """host 在但 _card_manager 缺失 → 返回 False（极端回退场景）"""
        self._register_card(fresh_registry)
        fake_host = type("Host", (), {})()  # 无 _card_manager/_window_id
        monkeypatch.setattr(fresh_registry, "_resolve_global_host", lambda: fake_host)
        assert fresh_registry.hide_floating_card_globally("autoloop-config") is False


class _FakeCardManager:
    """记录 hide_card 调用，最小 stub（避免 PySide6 真实构造开销）"""

    def __init__(self):
        self.calls: list = []

    def hide_card(self, card_id, window_id):
        self.calls.append((card_id, window_id))


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
