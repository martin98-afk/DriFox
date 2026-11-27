# -*- coding: utf-8 -*-
"""回归：占位重建卡片的「首报补偿方向」不得依赖未传播的布局几何。

## 用户可见现象

消息列表里不可见区域的 WebEngine 卡片被回收（换等高占位），用户再滚回去时
原位重建；重建后 viewer 的首次异步高度上报会触发外层锚定补偿
（``main_widget._on_message_card_height_changed``）。补偿是否执行，取决于
「这张卡是否整卡在视口上方」。

## 根因

判定原先直接用 ``sender.mapTo(container, ...)`` 读卡片顶部。重建走
``insertWidget`` + ``setFixedHeight``，Qt 布局要等到下一次事件循环才传播；
在此之前 ``mapTo`` 读到 ``y=0``。于是视口内 / 视口下方的重建卡被误判成
「在视口上方」，对真实高度差 delta 施加 ``value += delta`` —— 视口被凭空
拽走 delta 像素（实测 +120 / +300），并随滚动反复发生，表现为滚轮乱跳。

## 修复口径

重建侧在 ``_apply_placeholder_height`` 里用「插入位置之前的累计布局高度」
（不依赖待传播布局，各 widget 的 height() 已是稳定值）给出稳定估计
``_rebuild_above_px``；首报消费后即清除，之后恢复正常几何判定。
"""

import os
import sys
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from PyQt5.QtCore import QObject  # noqa: E402
from PyQt5.QtWidgets import QScrollArea, QVBoxLayout, QWidget  # noqa: E402

from app.main_widget import OpenAIChatToolWindow  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

BATCH = 40
VIEWER_H = 600
VIEWPORT_H = 600
SPACING = 8

_REAL_METHODS = (
    "_on_message_card_height_changed",
    "_should_follow_bottom",
    "_is_view_at_bottom",
    "_programmatic_scroll",
    "_landed_bottom_value",
    "_sync_scroll_maximum",
    "_install_batch_placeholder",
    "_take_batch_placeholder",
    "_pop_batch_overhead",
    "_apply_placeholder_height",
    "_layout_offset_before",
    "_batch_slot_gap",
    "_batch_layout_height",
    "_batch_fully_rendered",
    "_remove_placeholder_widget",
    "_is_widget_alive",
    "_clear_batch_placeholders",
    "_recount_rendered_cards",
    "_decr_rendered_count",
    "_unload_batch",
    "_render_message_to_card",
)


class _AsyncViewer(QWidget):
    """WebEngine 语义替身：真实布局子控件 + 异步高度（起步 40）。"""

    def __init__(self, parent, h=40):
        super().__init__(parent)
        self._h = int(h)
        self.setFixedHeight(self._h)

    def height(self):
        return self._h

    def minimumHeight(self):
        return self._h

    def setFixedHeight(self, h):
        self._h = int(h)
        super().setFixedHeight(int(h))

    def clamp_surface_height(self, h):
        return int(h)

    def sync_body_max_height(self, *a, **kw):
        pass


class _Host(QObject):
    """已初始化的 QObject 承载者：绑定方法槽才能收到信号（__new__ 桩会静默失败）。"""

    def __init__(self):
        super().__init__()
        for name in _REAL_METHODS:
            fn = getattr(OpenAIChatToolWindow, name, None)
            if fn is not None:
                setattr(self, name, types.MethodType(fn, self))


@pytest.fixture(scope="module")
def host(qapp):
    win = _Host()
    for key, value in {
        "_is_destroyed": False,
        "_window_id": "test-rebuild-scroll",
        "_programmatic_scroll_depth": 0,
        "_scroll_max_cache": None,
        "_is_virtual_recycling": False,
        "_user_intentionally_away_from_bottom": True,
        "_loading_session": False,
        "_bottom_anchor_deadline": 0.0,
        "_pending_scroll_to_bottom": False,
        "_is_loading_history_batches": False,
        "_jump_scroll_lock_until": 0.0,
        "_jump_target_card": None,
        "_pending_scroll_to_index": None,
        "_jump_settle_left": 0,
        "_last_visible_card_ids": set(),
        "_current_assistant_card": None,
        "_rendered_card_count": 0,
        "_recycle_lru_call_count": 0,
        "_incremental_visible_batch_count": 4,
        "_virtual_scroll_buffer": 1,
        "_max_rendered_cards": 99,
        "_min_rendered_cards": 4,
        "_unloaded_pids": [],
        "_pending_lazy_cards": [],
        "_lazy_batch_timer_active": False,
        "_is_streaming": False,
        "_initial_scroll_to_bottom": False,
        "_visible_batch_start": 0,
        "_visible_batch_end": BATCH,
        "_history_load_threshold": 48,
        "_last_layout_scan": None,
    }.items():
        setattr(win, key, value)

    for name in (
        "_log_render_quota",
        "_maybe_strong_recycle",
        "_recycle_lru_batches",
        "_sync_global_rendered_pages",
        "_process_next_lazy_batch",
        "_update_card_diff_stats",
        "_restore_meta_from_batch",
        "_create_message_widget",
        "_sync_single_card_width",
        "_schedule_bottom_snap",
        "_scroll_diag",
        "_scroll_to_bottom",
        "_maybe_schedule_lazy_process",
        "_enqueue_card_diff_stats",
        "_register_unloaded_pid",
        "_flush_batch_immediate_renders",
    ):
        setattr(win, name, lambda *a, **kw: None)
    win._try_detach_card_viewer = lambda card: False
    win._get_load_msg_extras_fn = lambda: None
    win._current_session_id_for_extras = lambda: "test"
    win._get_user_round_index_for_batch_index = lambda *a, **kw: 0
    win._is_jump_in_flight = lambda: False
    win._is_jump_scroll_locked = lambda: False
    win._user_prefix_cache = [0] * (BATCH + 1)

    scroll_area = QScrollArea()
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(SPACING)
    scroll_area.setWidget(container)
    scroll_area.setWidgetResizable(True)
    scroll_area.resize(520, VIEWPORT_H)
    win.chat_scroll_area = scroll_area
    win.chat_container = container
    win.chat_layout = layout

    import app.main_widget as _mw

    _mw.render_batch_to_assistant_card = lambda *a, **kw: None

    def _new_card(parent):
        card = MessageCard(role="assistant", parent=parent)
        card._lazy_rendered = True
        card._viewer_async_height = True
        card._streaming = False
        viewer = _AsyncViewer(card, VIEWER_H)
        card.viewer = viewer
        card.layout().addWidget(viewer)
        card._last_applied_viewer_height = VIEWER_H
        card._last_height_delta = 0
        card.is_layout_height_settled = lambda: True
        card.heightChanged.connect(win._on_message_card_height_changed)
        return card

    win._new_card = _new_card
    win._batch_cards = []
    win._batch_placeholders = {}
    win._batch_placeholder_overhead = {}
    win._message_batch = []
    for idx in range(BATCH):
        card = _new_card(container)
        card._message_index = idx
        layout.addWidget(card)
        win._batch_cards.append([card])
        win._message_batch.append([{"role": "assistant", "content": f"batch {idx}"}])

    def _fake_append(**kw):
        card = _new_card(container)
        card.viewer.setFixedHeight(40)
        card._last_applied_viewer_height = 40
        insert_index = kw.get("insert_index")
        win.chat_layout.insertWidget(
            win.chat_layout.count() if insert_index is None else insert_index, card
        )
        return card

    win._append_user_message = _fake_append
    win._append_assistant_message = _fake_append

    scroll_area.show()
    for _ in range(3):
        qapp.processEvents()
    container.adjustSize()
    for _ in range(3):
        qapp.processEvents()
    yield win
    scroll_area.hide()
    container.hide()


def _rebuild_and_report(host, qapp, target, hf, pump_before_report):
    """卸载 target 批 → 原位重建 → 模拟首报 hf，返回 (Δvalue, delta)。"""
    bar = host.chat_scroll_area.verticalScrollBar()
    card_h = host._batch_cards[0][0].height()
    bar.setValue(20 * (card_h + SPACING))
    qapp.processEvents()

    host._unload_batch(target)
    qapp.processEvents()
    ph = host._batch_placeholders.get(target)
    assert ph is not None, "前置条件：目标批应已卸载并留下等高占位"
    anchor = host.chat_layout.indexOf(ph)

    host._render_message_to_card(
        host._message_batch[target : target + 1],
        batch_offset=target,
        anchor_layout_index=anchor,
    )
    if pump_before_report:
        qapp.processEvents()
    card = host._batch_cards[target][0]

    v_before = bar.value()
    card._apply_viewer_height(hf)
    qapp.processEvents()
    return bar.value() - v_before, card


@pytest.mark.parametrize("hf,expect", [(720, 0), (480, 0), (900, 0)])
def test_below_viewport_rebuild_does_not_scroll(host, qapp, hf, expect):
    """视口下方的重建卡首报不得改动滚动值（布局未传播时也不得误判为上方）。"""
    delta_v, _card = _rebuild_and_report(host, qapp, 38, hf, pump_before_report=False)
    assert delta_v == expect, f"下方重建卡首报把视口拽走 {delta_v}px（期望 0）"


@pytest.mark.parametrize("hf,expect", [(720, 0), (480, 0)])
def test_in_viewport_rebuild_does_not_scroll(host, qapp, hf, expect):
    """视口内的重建卡首报不得改动滚动值。"""
    delta_v, _card = _rebuild_and_report(host, qapp, 20, hf, pump_before_report=False)
    assert delta_v == expect, f"视口内重建卡首报把视口拽走 {delta_v}px（期望 0）"


def test_above_viewport_rebuild_still_compensates(host, qapp):
    """视口上方的重建卡首报仍必须按真实增量补偿（不得为了防误拽而漏补）。"""
    delta_v, card = _rebuild_and_report(host, qapp, 2, 720, pump_before_report=False)
    assert card._layout_height_pinned is False
    assert delta_v == 120, f"上方重建卡真实增高 120px，补偿却是 {delta_v}px"


def test_rebuild_marker_consumed_once(host, qapp):
    """稳定估计是一次性令牌：首报消费后清除，后续上报回到常规几何判定。"""
    _delta_v, card = _rebuild_and_report(host, qapp, 38, 720, pump_before_report=False)
    assert getattr(card, "_rebuild_above_px", None) is None, "首报后必须清除重建位置估计"