# -*- coding: utf-8 -*-
"""UIEventBus：订阅/发布、异常隔离、插件级退订、事件隔离"""

import pytest

from app.core.ui_event_bus import (
    EV_TAB_SWITCHED,
    EV_THEME_CHANGED,
    UIEventBus,
)


@pytest.fixture()
def bus():
    b = UIEventBus.get_instance()
    yield b
    UIEventBus.reset_instance()


class TestEventBus:
    def test_subscribe_and_publish(self, bus):
        got = []
        bus.subscribe(EV_THEME_CHANGED, lambda p: got.append(p))
        bus.publish(EV_THEME_CHANGED, theme_id="dark", is_dark=True)
        assert got == [{"theme_id": "dark", "is_dark": True}]

    def test_event_isolation(self, bus):
        got = []
        bus.subscribe(EV_THEME_CHANGED, lambda p: got.append(p))
        bus.publish(EV_TAB_SWITCHED, tab_index=1)
        assert got == []

    def test_callback_exception_isolated(self, bus):
        calls = []

        def boom(_p):
            raise RuntimeError("boom")

        bus.subscribe(EV_THEME_CHANGED, boom)
        bus.subscribe(EV_THEME_CHANGED, lambda p: calls.append("ok"))
        bus.publish(EV_THEME_CHANGED)  # boom 不阻断 ok
        assert calls == ["ok"]

    def test_unsubscribe_plugin(self, bus):
        got = []
        bus.subscribe(EV_THEME_CHANGED, lambda p: got.append(1), plugin_name="demo")
        bus.unsubscribe_plugin("demo")
        bus.publish(EV_THEME_CHANGED)
        assert got == []

    def test_global_subscription_survives_plugin_unsub(self, bus):
        got = []
        bus.subscribe(EV_THEME_CHANGED, lambda p: got.append(1))  # 无 plugin_name
        bus.unsubscribe_plugin("demo")
        bus.publish(EV_THEME_CHANGED)
        assert got == [1]

    def test_subscription_count(self, bus):
        bus.subscribe(EV_THEME_CHANGED, lambda p: None, plugin_name="a")
        bus.subscribe(EV_THEME_CHANGED, lambda p: None, plugin_name="b")
        assert bus.subscriptions()[EV_THEME_CHANGED] == 2
