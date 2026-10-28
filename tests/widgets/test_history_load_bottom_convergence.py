# -*- coding: utf-8 -*-
"""回归测试：长历史会话加载后停在消息列表中间（而非置底）。

== 用户症状 ==
打开一条长历史会话，首屏渲染完成后视口停在中段，不是最新消息。
短会话正常，长会话（卡片高度异步上报持续数秒）必现 → 「经常卡在中间」。

== 根因 ==
`_ensure_at_bottom` 把「当前贴底」与「重试用尽」当作收敛结束，两处都
`_ensure_ab_active = False` 永久关掉事件驱动窗口。而历史会话的卡片高度由
WebEngine **异步**上报，一张一次、持续数秒；某一次重试恰好贴底就把窗口关掉，
之后 `_on_scroll_range_changed` 首行 `if not self._ensure_ab_active: return`
拦下全部剩余落高 → 后续卡片长高无人贴底 → 视口停在中段。

`_on_scroll_range_changed` 的固定 2.4s 保险丝同理：它把窗口长度拍死成一个
常量，长会话（实测可达 8.5s）超时后照样永久关窗。

== 修复 ==
1. `_ensure_at_bottom` 不再关闭窗口（窗口只由保险丝决定生命周期）。
2. `_on_scroll_range_changed` 命中即续期：rangeChanged 只在内容高真的变化时
   触发，「还在触发」等价于「还有卡片在落高」。窗口长度跟随真实工作量，
   落高停止后自然过期。

== 测试策略 ==
用假时钟 + 带 Qt clamp 语义的假滚动条驱动**真实的** `_do_scroll_to_bottom` /
`_maintain_bottom_anchor` / `_ensure_at_bottom` / `_on_scroll_range_changed`，
按真实节奏（16ms 帧、50ms 滚底定时器、100ms 锚定拍、300ms 重试）推进时间线。
断言最终落点距「已落地底」不超过 AT_BOTTOM_TOLERANCE。
"""

import sys
import types
from unittest import mock

import pytest

import app.main_widget as mw
from app.main_widget import AT_BOTTOM_TOLERANCE, OpenAIChatToolWindow

VIEWPORT_H = 200
_CLOCK = [None]


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


class _Hint:
    def __init__(self, h):
        self._h = h

    def height(self):
        return self._h


class _Bar:
    """带 Qt 语义的滚动条替身：setMaximum 缩小时自动钳 value 并触发信号。"""

    def __init__(self, maximum=1000, value=1000):
        self._max = maximum
        self._val = value
        self._v_handlers = []
        self._r_handlers = []

    def maximum(self):
        return self._max

    def value(self):
        return self._val

    def setMaximum(self, v):
        old = self._max
        self._max = max(0, v)
        if self._max != old:
            if self._val > self._max:
                self._val = self._max
                for h in self._v_handlers:
                    h(self._val)
            for h in self._r_handlers:
                h(0, self._max)

    def setValue(self, v):
        nv = max(0, min(v, self._max))
        if nv != self._val:
            self._val = nv
            for h in self._v_handlers:
                h(nv)


class _Container:
    """容器几何：已落地内容高 = 当前上界 + 视口（与真实布局同源）。"""

    def __init__(self, bar):
        self._bar = bar

    def height(self):
        return self._bar.maximum() + VIEWPORT_H

    def sizeHint(self):
        return _Hint(self._bar.maximum() + VIEWPORT_H)


def _make_window():
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    bar = _Bar()
    for k, v in {
        "_is_destroyed": False,
        "_programmatic_scroll_depth": 0,
        "_scroll_max_cache": None,
        "_pending_scroll_to_bottom": False,
        "_bottom_anchor_deadline": 0.0,
        "_bottom_anchor_timer": mock.MagicMock(),
        "_scroll_bottom_timer": mock.MagicMock(),
        "_ensure_ab_active": False,
        "_ensure_ab_active_until": 0.0,
        "_user_intentionally_away_from_bottom": False,
        "_jump_scroll_lock_until": 0.0,
        "_jump_target_card": None,
        "_pending_scroll_to_index": None,
        "_pending_scroll_to_batch": None,
        "_jump_settle_left": 0,
        "_loading_session": False,
        "_initial_scroll_to_bottom": True,
        "_is_streaming": False,
        "_history_load_threshold": 48,
        "_load_more_history_batches": mock.MagicMock(),
        "_virtual_scroll_timer": mock.MagicMock(),
        "_scroll_sync_timer": mock.MagicMock(),
        "_update_scroll_to_bottom_button": mock.MagicMock(),
    }.items():
        setattr(win, k, v)
    area = mock.MagicMock()
    area.verticalScrollBar.return_value = bar
    area.widget.return_value = _Container(bar)
    viewport = mock.MagicMock()
    viewport.height.return_value = VIEWPORT_H
    area.viewport.return_value = viewport
    win.chat_scroll_area = area
    bar._v_handlers.append(win._on_scroll_changed)
    bar._r_handlers.append(win._on_scroll_range_changed)
    return win, bar


def _drive_load(heights, t_end, sticky_ms=900):
    """驱动一轮「加载完成后卡片陆续落高」的时间线，返回最终 (value, landed)。

    Args:
        heights: [(秒, 新的 landed 高度)]，按时间升序。
        t_end: 时间线上限（秒）。
    """
    clock = _Clock()
    _CLOCK[0] = clock
    win, bar = _make_window()
    singles = []
    with (
        mock.patch.object(mw, "time", types.SimpleNamespace(monotonic=clock.monotonic)),
        mock.patch.object(
            mw.QTimer,
            "singleShot",
            staticmethod(lambda ms, cb: singles.append([clock.monotonic() + ms / 1000.0, ms, cb])),
        ),
    ):
        # T0：懒渲染队列排空 → sticky 置底（生产链路同款入口）
        win._scroll_to_bottom(sticky_ms=sticky_ms)
        t = 0.0
        hp = 0
        while t <= t_end:
            while hp < len(heights) and heights[hp][0] <= t:
                bar.setMaximum(heights[hp][1])
                hp += 1
            for item in [x for x in singles if x[0] <= clock.monotonic()]:
                singles.remove(item)
                item[2]()
            # 50ms 滚底定时器（单发）
            if win._scroll_bottom_timer.start.called:
                win._scroll_bottom_timer.start.reset_mock()
                clock.t += 0.05
                t += 0.05
                win._do_scroll_to_bottom()
                continue
            # 100ms 锚定拍（单发）
            if win._bottom_anchor_timer.start.called:
                win._bottom_anchor_timer.start.reset_mock()
                clock.t += 0.1
                t += 0.1
                win._maintain_bottom_anchor()
                continue
            clock.t += 0.016
            t += 0.016
        return bar.value(), win._landed_bottom_value(), win


# 长会话：15 张卡在 5.3s 内陆续落高（WebEngine 异步上报的真实量级）
_LONG = [(0.4 + i * 0.35, 1000 + (i + 1) * 900) for i in range(15)]
# 超长会话：24 张卡在 8.5s 内落高（远超旧实现的固定 2.4s 保险丝）
_VERY_LONG = [(0.3 + i * 0.35, 1000 + (i + 1) * 700) for i in range(24)]
# 短会话：5 张卡 0.6s 内落完（修复前后都应通过，防过度修正）
_SHORT = [(0.2 + i * 0.1, 1000 + (i + 1) * 900) for i in range(5)]


@pytest.mark.parametrize(
    "label,heights,t_end",
    [
        ("长会话 15 张 / 5.3s", _LONG, 10.0),
        ("超长会话 24 张 / 8.5s", _VERY_LONG, 12.0),
        ("短会话 5 张 / 0.6s", _SHORT, 8.0),
    ],
)
def test_history_load_converges_to_bottom(label, heights, t_end):
    """卡片陆续落高时视口必须持续追平底部，不得停在中段"""
    value, landed, win = _drive_load(heights, t_end)
    gap = landed - value
    assert abs(gap) <= AT_BOTTOM_TOLERANCE, (
        f"{label}：视口距底 {gap}px（上限 {AT_BOTTOM_TOLERANCE}）—— 停在消息列表中间"
    )


def test_event_window_not_closed_when_already_at_bottom():
    """贴底不得关闭事件驱动窗口（否则后续落高无人贴底）

    这是本 bug 的第一环：`_ensure_at_bottom` 上一次重试恰好贴底 → 旧实现
    `else: _ensure_ab_active = False` → 之后 rangeChanged 全被首行 return 拦下。
    """
    clock = _Clock()
    _CLOCK[0] = clock
    win, bar = _make_window()
    with mock.patch.object(mw, "time", types.SimpleNamespace(monotonic=clock.monotonic)):
        win._ensure_at_bottom(retries=0)
        assert win._ensure_ab_active is True, "贴底后窗口被关闭，后续落高会失去贴底通道"


def test_event_window_fuse_renewed_by_layout_height_change():
    """仍有落高时必须续期保险丝，不能按固定时长永久关窗

    旧实现把窗口长度拍成 2.4s 常量；长会话异步上报可达 8s+，超时后
    rangeChanged 直接关窗 return → 剩余卡片无人贴底。修复后命中即续期。
    """
    clock = _Clock()
    _CLOCK[0] = clock
    win, bar = _make_window()
    with mock.patch.object(mw, "time", types.SimpleNamespace(monotonic=clock.monotonic)):
        win._ensure_ab_active = True
        win._ensure_ab_active_until = clock.monotonic() + 2.4
        # 模拟超出原保险丝的落高事件（第 5 秒）
        clock.t += 5.0
        bar.setMaximum(9000)
        assert win._ensure_ab_active is True, "保险丝过期后被永久关闭，长会话剩余落高无人贴底"
        assert win._ensure_ab_active_until > clock.monotonic(), "命中落高应续期保险丝"


def test_event_window_can_be_closed_by_jump_lock():
    """跳转锁必须仍能关闭事件窗口，让 rangeChanged 停止自动置底

    续期只由「落高事件」驱动，跳转时窗口被显式关闭；关闭后 rangeChanged
    首行直接 return，落高不会把刚落好的跳转视口拽回底部。
    """
    clock = _Clock()
    _CLOCK[0] = clock
    win, bar = _make_window()
    with mock.patch.object(mw, "time", types.SimpleNamespace(monotonic=clock.monotonic)):
        win._ensure_ab_active = True
        win._ensure_ab_active_until = clock.monotonic() + 2.4
        win._jump_scroll_lock_until = clock.monotonic() + 5.0
        win._lock_scroll_after_jump()
        assert win._ensure_ab_active is False, "跳转上锁必须关窗"
        bar.setMaximum(9000)
    assert bar.value() == 1000, "跳转后落高不得把视口拽回底部"


def test_stale_range_change_does_not_follow_when_user_away():
    """窗口开着但用户已滚离：落高不得抢走阅读位置"""
    clock = _Clock()
    _CLOCK[0] = clock
    win, bar = _make_window()
    with mock.patch.object(mw, "time", types.SimpleNamespace(monotonic=clock.monotonic)):
        win._ensure_ab_active = True
        win._ensure_ab_active_until = clock.monotonic() + 2.4
        win._user_intentionally_away_from_bottom = True
        bar._val = 400
        bar.setMaximum(9000)
    assert bar.value() == 400


def test_away_guard_still_blocks_automatic_bottom_follow():
    """修复不得削弱 away 守卫：用户上滚阅读时落高不抢视口"""
    clock = _Clock()
    _CLOCK[0] = clock
    win, bar = _make_window()
    with mock.patch.object(mw, "time", types.SimpleNamespace(monotonic=clock.monotonic)):
        win._user_intentionally_away_from_bottom = True
        bar._val = 400  # 用户停在中段阅读
        win._ensure_ab_active = True
        win._ensure_ab_active_until = clock.monotonic() + 2.4
        bar.setMaximum(9000)
    assert bar.value() == 400, "用户阅读位置被自动置底抢走"


def test_event_driven_switch_off_keeps_legacy_behaviour():
    """DRIFOX_EVENT_SCROLL=0 回退开关下 rangeChanged 不参与贴底"""
    clock = _Clock()
    _CLOCK[0] = clock
    win, bar = _make_window()
    with (
        mock.patch.object(mw, "time", types.SimpleNamespace(monotonic=clock.monotonic)),
        mock.patch.object(mw, "STREAM_BOTTOM_EVENT_DRIVEN", False),
    ):
        win._ensure_ab_active = True
        win._ensure_ab_active_until = clock.monotonic() + 2.4
        bar.setMaximum(9000)
    assert bar.value() == 1000, "开关关闭时不应由 rangeChanged 贴底"
