# -*- coding: utf-8 -*-
"""回归：欢迎卡片 tab 条宿主宽度未就位时必须重试，不得停在初始高度

缺陷（2026-10-17 用户反馈）：进入软件时欢迎卡片底部空白，只有过一段时间
手动新建会话刷新才恢复。

根因：`MessageCard._sync_welcome_tab_host_height()` 在布局未收敛
（启动期首窗 / 隐藏页切回补渲）时读到 `host.width() <= 0`，直接 return。
此后**唯一的重算入口是 resizeEvent** —— 用户不动窗口是常态，宿主高度于是
永久停在构造初始值，与真实折行行数不符 → 卡片底部留一条空白。
新建会话触发整卡重建，此时布局早已收敛，故「刷新才恢复」。

修复：宽度未就位改为预约下一拍重试（QTimer 绑定 card 生命周期，卡片先销毁
则定时器随之析构，不会对悬垂控件取几何 → 不会 ACCESS_VIOLATION）。

本用例用 stub 宿主/布局，不依赖 Qt 布局真实收敛。
"""

from unittest.mock import MagicMock

from app.widgets.message_card import MessageCard  # noqa: E402


def _make_card(host_width: int, bar_height: int):
    """构造 _sync_welcome_tab_host_height 可运行的最小卡片实例。

    与既有 tests/widgets 同策略：`__new__` 绕过 __init__（依赖过重），
    手工补齐本方法触及的字段。
    """
    card = MessageCard.__new__(MessageCard)
    host = MagicMock()
    host.width.return_value = host_width
    host.height.return_value = 0  # 初始高度 0：模拟尚未设高
    bar = MagicMock()
    bar.heightForWidth.return_value = bar_height
    card._welcome_tab_host = host
    card._welcome_tabs_bar_layout = bar
    # QTimer(self) 需要真实 QObject parent：给一个自带 deleteLater 的桩，
    # 并让 timeout.connect 立即记录回调（单拍同步触发便于断言）
    card._welcome_tab_host_height_timer_active = False
    return card, host, bar


def test_width_zero_schedules_retry():
    """宽度 0 时不得静默放弃 —— 必须预约重试（否则高度永久停在初始值）"""
    card, host, _bar = _make_card(host_width=0, bar_height=32)
    scheduled = []

    class _Signal:
        def __init__(self):
            self.cb = None

        def connect(self, cb):
            self.cb = cb

    class _Timer:
        def __init__(self, parent):
            self.timeout = _Signal()

        def setSingleShot(self, v):
            pass

        def start(self, ms):
            scheduled.append(ms)

        def deleteLater(self):
            pass

    import app.widgets.message_card as mc_mod

    orig_timer = mc_mod.QTimer
    mc_mod.QTimer = _Timer
    try:
        card._sync_welcome_tab_host_height()
    finally:
        mc_mod.QTimer = orig_timer
    assert scheduled, "宽度 0 时必须预约一拍重试，原实现直接 return 导致高度永久错误"
    host.setFixedHeight.assert_not_called()


def test_width_positive_sets_height():
    """守护：宽度就位时仍按 heightForWidth 设高（修复不得改变正常路径）"""
    card, host, bar = _make_card(host_width=886, bar_height=32)
    card._sync_welcome_tab_host_height()
    bar.heightForWidth.assert_called_once_with(886)
    host.setFixedHeight.assert_called_once_with(32)


def test_retry_is_idempotent_within_same_frame():
    """同一帧内重复调用不得叠加多个定时器（并发调度会重复设高触发抖动）"""
    card, _host, _bar = _make_card(host_width=0, bar_height=32)
    starts = []

    class _Signal:
        def __init__(self):
            pass

        def connect(self, cb):
            pass

    class _Timer:
        def __init__(self, parent):
            self.timeout = _Signal()

        def setSingleShot(self, v):
            pass

        def start(self, ms):
            starts.append(ms)

        def deleteLater(self):
            pass

    import app.widgets.message_card as mc_mod

    orig_timer = mc_mod.QTimer
    mc_mod.QTimer = _Timer
    try:
        card._sync_welcome_tab_host_height()
        card._sync_welcome_tab_host_height()
        card._sync_welcome_tab_host_height()
    finally:
        mc_mod.QTimer = orig_timer
    assert len(starts) == 1, f"同帧三次调用只应预约一次，实际 {len(starts)}"
