# -*- coding: utf-8 -*-
"""主程序事件埋点：theme_manager / tab 切换 / 卡片显隐 的 publish 调用点存在且 payload 正确

用 monkeypatch 拦截 publish，不构造真实 Qt 窗口——埋点函数级验证。
"""

from app.core.ui_event_bus import (
    EV_CARD_VISIBILITY_CHANGED,
    EV_TAB_SWITCHED,
    EV_THEME_CHANGED,
    UIEventBus,
)


def test_theme_manager_publishes_on_change(monkeypatch):
    from app.utils.theme_manager import ThemeManager

    published = []
    monkeypatch.setattr(
        UIEventBus,
        "publish",
        classmethod(lambda cls, ev, **kw: published.append((ev, kw))),
    )
    tm = ThemeManager()
    monkeypatch.setattr(tm, "get_current_theme_id", lambda: "midnight")
    monkeypatch.setattr(tm, "get_current_theme", lambda: {"name": "午夜"})
    monkeypatch.setattr(tm, "is_light_theme", lambda theme_id=None: True)  # 防 _load_themes 文件 IO
    tm.on_theme_changed()
    assert published and published[-1][0] == EV_THEME_CHANGED
    assert published[-1][1]["theme_id"] == "midnight"
    assert published[-1][1]["is_dark"] is False


def test_card_manager_publishes_visibility_on_show(monkeypatch):
    from unittest.mock import MagicMock

    from app.widgets.cards.card_manager import CardManager, ContainerType

    published = []
    monkeypatch.setattr(
        UIEventBus,
        "publish",
        classmethod(lambda cls, ev, **kw: published.append((ev, kw))),
    )
    CardManager.reset_instance()
    cm = CardManager.get_instance()
    cm._ensure_window_initialized("w1")
    widget = MagicMock()
    widget.windowTitle.return_value = "test"
    cm._window_data["w1"]["containers"]["mycard"] = ContainerType.TOP
    cm._window_data["w1"]["cards"][ContainerType.TOP]["mycard"] = widget
    cm.show_card("mycard", "w1")
    assert any(p[0] == EV_CARD_VISIBILITY_CHANGED for p in published), f"missing visibility event in {published}"
    last = [p for p in published if p[0] == EV_CARD_VISIBILITY_CHANGED][-1]
    assert last[1] == {"card_id": "mycard", "window_id": "w1", "visible": True}
    CardManager.reset_instance()


def test_card_manager_publishes_visibility_on_hide(monkeypatch):
    from unittest.mock import MagicMock

    from app.widgets.cards.card_manager import CardManager, ContainerType

    published = []
    monkeypatch.setattr(
        UIEventBus,
        "publish",
        classmethod(lambda cls, ev, **kw: published.append((ev, kw))),
    )
    CardManager.reset_instance()
    cm = CardManager.get_instance()
    cm._ensure_window_initialized("w2")
    widget = MagicMock()
    widget.windowTitle.return_value = "test"
    cm._window_data["w2"]["containers"]["mycard"] = ContainerType.TOP
    cm._window_data["w2"]["cards"][ContainerType.TOP]["mycard"] = widget
    cm.show_card("mycard", "w2")
    cm.hide_card("mycard", "w2")
    hide_events = [p for p in published if p[0] == EV_CARD_VISIBILITY_CHANGED]
    assert any(p[1].get("visible") is False for p in hide_events), f"missing hide visibility event: {published}"
    CardManager.reset_instance()


def test_ui_event_bus_publish_isolates_callback_exceptions(monkeypatch):
    """单个订阅者抛异常不影响其他订阅者"""
    bus = UIEventBus.get_instance()
    bus._subs.clear()
    received = []

    def bad_cb(_):
        raise RuntimeError("boom")

    def good_cb(p):
        received.append(p)

    bus.subscribe("test_event", bad_cb, plugin_name="bad")
    bus.subscribe("test_event", good_cb, plugin_name="good")
    bus.publish("test_event", foo="bar")
    assert received == [{"foo": "bar"}]
    bus._subs.clear()