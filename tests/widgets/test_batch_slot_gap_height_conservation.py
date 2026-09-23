# -*- coding: utf-8 -*-
"""多卡批次回收的「槽内间距」高度守恒回归。

## 用户可见现象

长对话上下滚动时滚轮位置乱跳（视口内容整体位移），且跳动量随会话长度
累积。单卡批次不出现，多卡批次（user + assistant 同批 / 连续 assistant）
必现。

## 根因

一个含 n 张卡的批次在 ``chat_layout`` 里占 **n 个槽 + n-1 条 spacing**，
卸载后只留 **1 个**占位槽。而卸载侧按「卡高之和」建等高占位，漏掉那
n-1 条间距 → 容器总高在回收瞬间变矮 ``(n-1) × spacing``：

    per=2（23 个占位）  total 24632 → 24448   (delta -184 = 23×1×8)
    per=3（23 个占位）  total 36952 → 36584   (delta -368 = 23×2×8)

差额被当作「布局高度变化」由后续补偿吃掉 → 视口内容整体上移 = 跳变。
多卡批次是真实会话常态（一轮 user 追问 + 多段 assistant / 工具结果），
所以每次回收都漏，误差随滚动累积。

## 修法

- 卸载 / 建占位统一用 ``_batch_layout_height`` 口径（卡高 + 槽内间距）
- 重建侧 ``_apply_placeholder_height`` 先扣掉 gap 再分配给各卡（插回布局
  后 Qt 自动补回间距，不扣会让总高凭空多出 gap）

## 手工复现

    python tests/debug/probe_multi_card_spacing.py
"""

import os
import sys
from unittest.mock import MagicMock

import pytest
from PyQt5.QtWidgets import QScrollArea, QVBoxLayout, QWidget

from app.main_widget import OpenAIChatToolWindow
from app.widgets.message_card import MessageCard

BATCH = 40
CARD_H = 300
VIEWPORT_H = 400
SPACING = 8


def _make_window(qapp, per=2):
    """装配含多卡批次的最小窗口（spacing 必须非零，否则测不到漏账）。"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    for key, value in {
        "_is_destroyed": False,
        "_programmatic_scroll_depth": 0,
        "_scroll_max_cache": None,
        "_is_virtual_recycling": False,
        "_user_intentionally_away_from_bottom": False,
        "_loading_session": False,
        "_bottom_anchor_deadline": 0.0,
        "_pending_scroll_to_bottom": False,
        "_is_loading_history_batches": False,
        "_last_visible_card_ids": set(),
        "_restore_queue": [],
        "_current_assistant_card": None,
        "_layout_epoch": 0,
        "_node_user_cards_cache": None,
        "_rendered_card_count": 0,
        "_recycle_lru_call_count": 0,
        "_incremental_visible_batch_count": 8,
        "_virtual_scroll_buffer": 1,
        "_max_rendered_cards": 12,
        "_min_rendered_cards": 6,
        "_unloaded_pids": [],
        "_pending_lazy_cards": [],
        "_lazy_batch_timer_active": False,
        "_history_load_threshold": 48,
        "_initial_scroll_to_bottom": True,
        "_suppress_scroll_sync_count": 0,
        "_visible_batch_start": 0,
        "_visible_batch_end": BATCH,
    }.items():
        setattr(win, key, value)

    win._log_render_quota = MagicMock()
    win._maybe_strong_recycle = MagicMock()
    win._recycle_lru_batches = MagicMock()
    win._sync_global_rendered_pages = MagicMock()
    win._process_next_lazy_batch = MagicMock()
    win._try_detach_card_viewer = lambda card: False
    win._is_widget_alive = lambda w: w is not None
    win._get_load_msg_extras_fn = lambda: None
    win._current_session_id_for_extras = lambda: "s1"
    win._get_user_round_index_for_batch_index = MagicMock(return_value=0)
    win._update_card_diff_stats = MagicMock()
    win._restore_meta_from_batch = MagicMock()
    win._create_message_widget = MagicMock(return_value=None)
    win._sync_single_card_width = MagicMock()

    scroll_area = QScrollArea()
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(SPACING)
    scroll_area.setWidget(container)
    scroll_area.resize(500, VIEWPORT_H)
    win.chat_scroll_area = scroll_area
    win.chat_container = container
    win.chat_layout = layout

    batch_cards, message_batch = [], []
    for idx in range(BATCH):
        cards = []
        for j in range(per):
            card = MessageCard(role="user" if j == 0 else "assistant", parent=container)
            card.setFixedHeight(CARD_H)
            card._message_index = idx
            card._lazy_rendered = True
            layout.addWidget(card)
            cards.append(card)
        batch_cards.append(cards)
        message_batch.append([{"role": "assistant", "content": f"batch {idx}"}])
    win._batch_cards = batch_cards
    win._message_batch = message_batch
    win._batch_placeholders = {}
    win._user_prefix_cache = [0] * (BATCH + 1)

    def _fake_append(**kw):
        card = MessageCard(role="assistant", parent=container)
        card._message_index = kw.get("insert_index") or 0
        card._lazy_rendered = True
        win._add_chat_widget(card, insert_index=kw.get("insert_index"))
        return card

    win._append_user_message = _fake_append
    win._append_assistant_message = _fake_append
    win._add_chat_widget = lambda w, insert_index=None: layout.insertWidget(
        layout.count() if insert_index is None else insert_index, w
    )

    scroll_area.show()
    for _ in range(3):
        qapp.processEvents()
    container.adjustSize()
    for _ in range(3):
        qapp.processEvents()
    return win


def test_batch_slot_gap_counts_inner_spacing(qapp):
    """槽内间距 = (n-1) × spacing；单卡批次为 0。"""
    win = _make_window(qapp, per=1)
    assert win._batch_slot_gap(1) == 0, "单卡批次无内部间距"
    assert win._batch_slot_gap(2) == SPACING
    assert win._batch_slot_gap(3) == 2 * SPACING


@pytest.mark.parametrize("per", [1, 2, 3])
def test_recycle_multi_card_batch_preserves_total_height(qapp, per):
    """回收多卡批次后容器总高必须守恒（漏账 = (n-1)×spacing×批次数）。

    未修复实测：per=2 → -184px，per=3 → -368px。
    """
    win = _make_window(qapp, per=per)
    sb = win.chat_scroll_area.verticalScrollBar()
    sb.setValue(20 * (per * CARD_H + (per - 1) * SPACING + SPACING))
    qapp.processEvents()

    before = win.chat_container.sizeHint().height()
    win._recycle_out_of_view_batches()
    qapp.processEvents()
    qapp.processEvents()
    after = win.chat_container.sizeHint().height()

    assert win._batch_placeholders, "前置条件：应有占位留下"
    assert after == pytest.approx(before, abs=1), (
        f"per={per} 回收后总高必须守恒：{before} → {after}（差 {after - before}px，"
        f"漏账应为 {len(win._batch_placeholders) * (per - 1) * SPACING}px = 槽内间距未计入）"
    )


def test_apply_placeholder_height_excludes_gap(qapp):
    """还原高度必须先扣槽内间距：插回布局后 Qt 自动补回，不扣会让总高虚增。"""
    win = _make_window(qapp, per=1)

    def _mk():
        card = MessageCard(role="assistant", parent=win.chat_container)
        win.chat_layout.addWidget(card)
        return card

    c1, c2 = _mk(), _mk()
    # 两张卡：布局高度 = 300 + 300 + 8（Qt 会自动补 8）→ 卡高应各 300，不是 304
    win._apply_placeholder_height([c1, c2], 2 * CARD_H + SPACING)
    assert c1.height() == CARD_H, f"还原高度不应含 gap：{c1.height()} ≠ {CARD_H}"
    assert c2.height() == CARD_H, f"还原高度不应含 gap：{c2.height()} ≠ {CARD_H}"


def test_scroll_does_not_drift_on_repeated_recycle(qapp):
    """连续上滚 + 每步一轮回收：滚动值零漂移（用户视角的「滚轮乱跳」）。"""
    win = _make_window(qapp, per=2)
    sb = win.chat_scroll_area.verticalScrollBar()
    sb.setValue(sb.maximum())
    qapp.processEvents()

    for step in range(10):
        sb.setValue(max(0, sb.value() - 300))
        qapp.processEvents()
        expected = sb.value()
        win._recycle_out_of_view_batches()
        qapp.processEvents()
        qapp.processEvents()
        drift = sb.value() - expected
        assert drift == 0, f"step {step}: 回收引起滚动漂移 {drift}px（期望 {expected}，实际 {sb.value()}）"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
