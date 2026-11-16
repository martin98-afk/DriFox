# -*- coding: utf-8 -*-
"""UIEventBus：订阅/发布、异常隔离、插件级退订、事件隔离"""

import pytest

from app.core.infra.ui_event_bus import (
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


class TestLeakFixP1_6:
    """[LEAK-FIX P1-6] 绑定方法弱持有：宿主销毁即失效，lambda/闭包仍强持有。"""

    class _Host:
        def __init__(self):
            self.last = None

        def on_theme(self, payload):
            self.last = payload

    def test_bound_method_host_destroyed_auto_purges(self, bus):
        """泄漏主场景：宿主销毁后弱引用失效，publish 惰性清理，持有数归零。

        改前口径：_subs 强引用绑定方法，宿主销毁后 subscriptions() 恒 1，
        旧窗口整树被总线常驻（P1-6 泄漏本体）。
        """
        import gc

        host = self._Host()
        bus.subscribe(EV_THEME_CHANGED, host.on_theme)
        assert bus.subscriptions()[EV_THEME_CHANGED] == 1  # 持有数：订阅后 1

        del host
        gc.collect()
        bus.publish(EV_THEME_CHANGED, theme_id="dark")  # 触发惰性清理；死回调不投递
        assert bus.subscriptions().get(EV_THEME_CHANGED, 0) == 0  # 持有数：清理后 0

    def test_lambda_stays_strong_referenced(self, bus):
        """防提前失效回归：lambda/闭包不可安全弱引用，必须维持强持有。"""
        got = []
        bus.subscribe(EV_THEME_CHANGED, lambda p: got.append(p["theme_id"]))
        bus.publish(EV_THEME_CHANGED, theme_id="light")
        assert got == ["light"]

    def test_unsubscribe_bound_method_via_weak_form(self, bus):
        """unsubscribe 弱形态兼容：绑定方法按 (__self__, __func__) 身份比对。

        绑定方法每次属性访问产生新对象，`cur is callback` 恒 False；
        手动退订通道（主程序对象生命周期管理）不得因此失效。
        """
        host = self._Host()
        bus.subscribe(EV_THEME_CHANGED, host.on_theme)
        bus.unsubscribe(EV_THEME_CHANGED, host.on_theme)
        assert bus.subscriptions().get(EV_THEME_CHANGED, 0) == 0
        bus.publish(EV_THEME_CHANGED, theme_id="dark")  # 已退订：不投递
        assert host.last is None

    def test_subscription_count_ignores_dead_weakrefs(self, bus):
        """计数口径：已死弱引用不计入 subscriptions()（未 publish 也不计入）。"""
        import gc

        host = self._Host()
        bus.subscribe(EV_THEME_CHANGED, host.on_theme)
        del host
        gc.collect()
        # 不经 publish 触发清理，计数也应视为 0（死引用不占额）
        assert bus.subscriptions().get(EV_THEME_CHANGED, 0) == 0
