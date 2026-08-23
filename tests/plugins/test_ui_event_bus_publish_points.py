# -*- coding: utf-8 -*-
"""主程序事件埋点：theme_manager / tab 切换 / 卡片显隐 的 publish 调用点存在且 payload 正确
（用 monkeypatch 拦截 publish，不构造真实 Qt 窗口——埋点函数级验证）"""

import pytest

from app.core.ui_event_bus import (
    EV_CARD_VISIBILITY_CHANGED,
    EV_TAB_SWITCHED,
    EV_THEME_CHANGED,
    UIEventBus,
)


def test_theme_manager_publishes_on_change(monkeypatch):
    published = []
    monkeypatch.setattr(UIEventBus, "publish", classmethod(lambda cls, ev, **kw: published.append((ev, kw))))
    from app.utils.theme_manager import ThemeManager

    tm = ThemeManager()
    monkeypatch.setattr(tm, "get_current_theme_id", lambda: "midnight")
    monkeypatch.setattr(tm, "get_current_theme", lambda: {"name": "午夜"})
    monkeypatch.setattr(tm, "is_light_theme", lambda theme_id=None: True)  # 防 _load_themes 文件 IO
    tm.on_theme_changed()
    assert published and published[-1][0] == EV_THEME_CHANGED
    assert published[-1][1]["theme_id"] == "midnight"
    assert published[-1][1]["is_dark"] is False


def test_card_manager_publishes_visibility(monkeypatch):
    from app.widgets.cards.card_manager import CardManager, ContainerType

    published = []
    monkeypatch.setattr(UIEventBus, "publish", classmethod(lambda cls, ev, **kw: published.append((ev, kw))))
    CardManager.reset_instance()
    cm = CardManager.get_instance()
    # 直接调内部成功路径：注册并显示一张卡
    cm._ensure_window_initialized("w1")
    cm._window_data["w1"]["containers"]["mycard"] = ContainerType.TOP
    fake_widget = type("FakeW", (), {"show_card": lambda self: None, "setVisible": lambda self, v: None, "windowTitle": staticmethod(lambda: "x")})()
    cm._window_data["w1"]["cards"][ContainerType.TOP]["mycard"] = fake_widget
    cm.show_card("mycard", "w1")
    assert any(p[0] == EV_CARD_VISIBILITY_CHANGED and p[1].get("visible") is True for p in published)


def test_event_constants_distinct():
    """事件常量字符串值各不相同（避免事件路由错位）"""
    constants = {EV_THEME_CHANGED, EV_TAB_SWITCHED, EV_CARD_VISIBILITY_CHANGED}
    assert len(constants) == 3
