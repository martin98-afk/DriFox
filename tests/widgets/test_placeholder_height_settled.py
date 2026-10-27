# -*- coding: utf-8 -*-
"""回归：占位高度必须取「已落定」的真实高度（滚轮乱跳 / 跳转漂移主因）。

## 用户可见现象

① 消息卡片滚出视野被回收、再滚回来时，滚轮位置乱跳：明明只滚了两格，视口
   却整屏挪走，往回滚又要滚好几次才回到原处。

② 从历史问题列表跳转，常常要跳两次才落到目标问题。

## 根因链（探针实测）

`_lazy_rendered` 在 viewer **创建完成**时就置 True，而 viewer 的真实高度要等
JS 渲染完异步上报（CodeWebViewer 数百 ms，纯 Qt 渲染器一拍）。这段窗口里
`card.height()` 仍是起步值 40px。

`_batch_fully_rendered` 只看 `_lazy_rendered`，于是：

    1. `_recycle_out_of_view_batches` 第 1 步给视口 ±8 批全部 ensure_rendered
       （`_lazy_rendered` 立刻 True，高度仍 40）
    2. 渲染计数超配额 → 下一帧 `_recycle_lru_batches` 淘汰距视口 >2 的批次
       —— 正好是刚建好的 ±3~±8
    3. 守卫通过 → `_install_batch_placeholder(card.height()=40)`
    4. 用户滚回该区 → 重建 pin 到 40 → viewer 落高 800/张
       → 视口内容整体位移数千 px

    回收侧量到批次高 = 280（真实 4840，低估 4560px）
    重建落高后视口顶 anchor 卡 y() 1904 → 6464

## 修复

`MessageCard.is_layout_height_settled()` 给出显式落定状态，`_batch_fully_rendered`
必须同时满足「已懒渲染」与「高度已落定」才允许回收。未落定的批次整批保留，
下一个回收周期高度落定后自然可卸。
"""

import sys
from unittest.mock import MagicMock

import pytest
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QVBoxLayout, QWidget

from app.main_widget import JUMP_SETTLE_MAX_TRIES, OpenAIChatToolWindow
from app.widgets.message_card import MessageCard


def _make_win():
    """`__new__` 最小桩（同 test_recycle_rendered_guard.py 法）。"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._is_widget_alive = lambda w: w is not None
    win._batch_cards = []
    win._batch_placeholders = {}
    win._last_visible_card_ids = set()
    win._rendered_card_count = 0
    win._unloaded_pids = []
    win._current_assistant_card = None
    win._decr_rendered_count = MagicMock()
    win._register_unloaded_pid = MagicMock()
    win._try_detach_card_viewer = lambda card: False
    win._install_batch_placeholder = MagicMock(return_value=True)
    win.chat_layout = MagicMock()
    win.chat_layout.indexOf.return_value = 0
    return win


class _FakeCodeViewer:
    """CodeWebViewer 替身（异步落高语义）。"""

    def __init__(self, height: int):
        self._h = int(height)

    def height(self) -> int:
        return self._h


def _card(*, lazy: bool = True, applied: int = 800, viewer_h: int = 800, **extra) -> MessageCard:
    card = MessageCard(role="assistant")
    card._lazy_rendered = lazy
    card._viewer_async_height = True
    if lazy:
        card.viewer = _FakeCodeViewer(viewer_h)
        card._last_applied_viewer_height = applied
    for key, value in extra.items():
        setattr(card, key, value)
    return card


# ── 落定判据 ────────────────────────────────────────────────────────


def test_settled_when_rendered_and_height_applied(qapp):
    """已渲染 + viewer 高度与已应用高度一致 → 落定。"""
    assert _card().is_layout_height_settled() is True


def test_not_settled_when_height_still_at_start_value(qapp):
    """刚懒渲染完、高度还在起步 40px → 未落定（占位高度会失真）。"""
    card = _card(applied=40, viewer_h=40)
    assert card.is_layout_height_settled() is False, "起步高度不是可信高度"


def test_not_settled_when_viewer_missing(qapp):
    """viewer 已摘（池化 detach）→ 未落定。"""
    card = _card()
    card.viewer = None
    assert card.is_layout_height_settled() is False


def test_not_settled_while_height_track_is_running(qapp):
    """流式高度追踪期间每拍都在改高度，量到的值下一拍就作废。"""
    card = _card()
    tick = MagicMock()
    tick.isActive.return_value = True
    card._stream_height_tick = tick
    assert card.is_layout_height_settled() is False


def test_not_settled_when_viewer_lags_applied_height(qapp):
    """viewer 高度滞后于已应用高度（布局还没传播）→ 未落定。"""
    assert _card(applied=800, viewer_h=640).is_layout_height_settled() is False


def test_not_settled_in_resize_preview(qapp):
    """resize preview 期间高度只记 _pending_viewer_height，viewer 停在旧值。"""
    card = _card(_resize_preview_mode=True)
    assert card.is_layout_height_settled() is False


def test_not_settled_with_pending_height(qapp):
    """有未应用的 pending 高度 → 未落定。"""
    card = _card(_pending_viewer_height=1200)
    assert card.is_layout_height_settled() is False


def test_user_bubble_settled_once_viewer_exists(qapp):
    """user 气泡（PlainTextViewer）同步定型：viewer 已建即落定。"""
    card = MessageCard(role="user")
    card._lazy_rendered = True
    assert card.is_layout_height_settled() is False, "viewer 未建时不落定"
    card.viewer = _FakeCodeViewer(90)
    assert card.is_layout_height_settled() is True


def test_qt_renderer_settled_without_async_window(qapp):
    """纯 Qt 渲染器（_viewer_async_height=False）无异步落高窗，直接放行。"""
    card = MessageCard(role="assistant")
    card._lazy_rendered = True
    card._viewer_async_height = False
    card.viewer = _FakeCodeViewer(600)
    assert card.is_layout_height_settled() is True


# ── 回收守卫接线 ────────────────────────────────────────────────────


def test_recycle_guard_rejects_stale_height_batch(qapp):
    """高度未落定 → `_batch_fully_rendered` 判 False，批次整批保留。"""
    win = _make_win()
    stale = _card(applied=40, viewer_h=40)
    assert win._batch_fully_rendered([stale]) is False


def test_recycle_guard_accepts_settled_batch(qapp):
    """高度已落定 → 允许回收。"""
    win = _make_win()
    assert win._batch_fully_rendered([_card()]) is True


def test_unload_batch_keeps_stale_height_batch(qapp):
    """未落高的批次即使 `_lazy_rendered=True` 也不得卸载（占位会失真）。"""
    win = _make_win()
    stale = _card(applied=40, viewer_h=40)
    win._batch_cards = [[stale]]

    removed = win._unload_batch(0)

    assert removed == 0
    assert win._batch_cards[0] == [stale], "高度未落定的批次必须保留 UI"
    win._install_batch_placeholder.assert_not_called()


# ── 跳转落点收敛链 ──────────────────────────────────────────────────


class _JumpHarness:
    """承载跳转收敛链的最小载体（真实几何用 MagicMock 顶替）。"""

    _apply_jump_target_now = OpenAIChatToolWindow._apply_jump_target_now
    _resolve_jump_target_value = OpenAIChatToolWindow._resolve_jump_target_value
    _schedule_jump_settle = OpenAIChatToolWindow._schedule_jump_settle
    _jump_settle_step = OpenAIChatToolWindow._jump_settle_step
    _programmatic_scroll = OpenAIChatToolWindow._programmatic_scroll
    _sync_scroll_maximum = lambda self: self.chat_scroll_area.verticalScrollBar().maximum()  # noqa: E731


def _jump_harness(ys):
    """ys = 逐拍读到的 card.y() 序列（模拟布局收敛过程）。"""
    h = _JumpHarness()
    h._programmatic_scroll_depth = 0
    h._is_widget_alive = lambda w: True
    card = MagicMock()
    card.y.side_effect = list(ys) + [ys[-1]] * 20
    h._jump_target_card = card
    h._jump_settle_left = JUMP_SETTLE_MAX_TRIES
    h._jump_settle_last_value = None
    bar = MagicMock()
    bar.maximum.return_value = 100000
    bar.value.return_value = 0
    area = MagicMock()
    area.verticalScrollBar.return_value = bar
    h.chat_scroll_area = area
    return h, bar


def test_jump_settle_corrects_stale_geometry(monkeypatch):
    """同栈读到的 y 是陈旧值（0），收敛后是 1232 → 收敛链必须补到 1232。"""
    fired: list = []
    monkeypatch.setattr(QTimer, "singleShot", lambda ms, cb: fired.append(cb))

    h, bar = _jump_harness([0, 1232, 1232])
    bar.value.return_value = 999  # 与首次落点（0）不同，确保真会写一次
    h._apply_jump_target_now()  # 首次落点：陈旧几何 0
    h._schedule_jump_settle()

    assert bar.setValue.call_args_list[-1][0][0] == 0, "首次落点用的是陈旧几何"

    for _ in range(6):
        if not fired:
            break
        fired.pop(0)()

    values = [c[0][0] for c in bar.setValue.call_args_list]
    assert values[-1] == 1232, f"收敛后必须落到真实位置，实际序列={values}"
    assert h._jump_target_card is None, "收敛完成必须释放目标卡引用"


def test_jump_settle_stops_on_stable_geometry(monkeypatch):
    """连续两拍读到同一 y → 立即停，不空转到预算用完。"""
    fired: list = []
    monkeypatch.setattr(QTimer, "singleShot", lambda ms, cb: fired.append(cb))

    h, bar = _jump_harness([500])
    h._apply_jump_target_now()
    h._schedule_jump_settle()

    for _ in range(10):
        if not fired:
            break
        fired.pop(0)()

    assert h._jump_target_card is None
    assert not fired, f"几何稳定后不应继续排拍，剩余 {len(fired)} 拍"


def test_jump_settle_aborts_when_card_destroyed(monkeypatch):
    """目标卡在收敛途中被回收 → 链自行终止，不再写滚动值。"""
    fired: list = []
    monkeypatch.setattr(QTimer, "singleShot", lambda ms, cb: fired.append(cb))

    h, bar = _jump_harness([0, 800])
    h._apply_jump_target_now()
    h._schedule_jump_settle()
    calls_before = bar.setValue.call_count

    h._is_widget_alive = lambda w: False
    fired.pop(0)()

    assert h._jump_target_card is None
    assert bar.setValue.call_count == calls_before, "目标卡已销毁不得再动滚动条"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
