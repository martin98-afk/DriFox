# -*- coding: utf-8 -*-
"""探针：多卡批次回收的 spacing 漏账。

    python tests/debug/probe_multi_card_spacing.py

假设：`_install_batch_placeholder` 用「卡高之和」建单个等高占位，
但原批次占着 k 个 layout 槽 → k-1 个 spacing。占位只有 1 个槽，
总高因此少 (k-1) × spacing。等高占位被当成「总高不变」而跳过滚动补偿，
差额全部体现为视口位移。
"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock  # noqa: E402

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication([])

from app.main_widget import OpenAIChatToolWindow  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

BATCH = 40
CARD_H = 300
VIEWPORT_H = 400
SPACING = 8
CARDS_PER_BATCH = 2  # user + assistant，真实场景常态


def build_window(spacing=SPACING, per=CARDS_PER_BATCH):
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
    layout.setSpacing(spacing)
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

    # 重建桩：一张 user + 一张 assistant
    def _fake_append_user(**kw):
        card = MessageCard(role="user", parent=container)
        card._lazy_rendered = True
        win._add_chat_widget(card, insert_index=kw.get("insert_index"))
        return card

    def _fake_append_assistant(**kw):
        card = MessageCard(role="assistant", parent=container)
        card._lazy_rendered = True
        win._add_chat_widget(card, insert_index=kw.get("insert_index"))
        return card

    win._append_user_message = _fake_append_user
    win._append_assistant_message = _fake_append_assistant
    win._add_chat_widget = lambda w, insert_index=None: layout.insertWidget(
        layout.count() if insert_index is None else insert_index, w
    )

    scroll_area.show()
    for _ in range(3):
        app.processEvents()
    container.adjustSize()
    for _ in range(3):
        app.processEvents()
    return win


def main():
    """修复前（占位只记卡高之和）：
        per=2: total 24632 -> 24448 (delta -184)
        per=3: total 36952 -> 36584 (delta -368)
    修复后：delta 恒为 0（delta = -占位数 × (per-1) × spacing）。
    """
    for per in (1, 2, 3):
        win = build_window(per=per)
        sb = win.chat_scroll_area.verticalScrollBar()
        sb.setValue(20 * (per * CARD_H + (per - 1) * SPACING + SPACING))
        app.processEvents()
        t0 = win.chat_container.sizeHint().height()
        v0 = sb.value()
        win._recycle_out_of_view_batches()
        app.processEvents()
        app.processEvents()
        t1 = win.chat_container.sizeHint().height()
        placeholders = sorted(win._batch_placeholders.keys())
        print(f"per={per}: 占位数={len(placeholders)}")
        print(f"        total {t0} -> {t1}  (delta {t1-t0:+d})")
        print(f"        value {v0} -> {sb.value()}  (drift {sb.value()-v0:+d})")
        # 理论漏账
        print(f"        理论漏账 = 占位数 × (per-1) × spacing = {len(placeholders)*(per-1)*SPACING}")
        print()

    # 连续上滚漂移
    win = build_window(per=2)
    sb = win.chat_scroll_area.verticalScrollBar()
    sb.setValue(sb.maximum())
    app.processEvents()
    print("=== 连续上滚（每步 300px + 一轮回收）===")
    total_drift = 0
    for step in range(12):
        v = max(0, sb.value() - 300)
        sb.setValue(v)
        app.processEvents()
        expected = sb.value()
        win._recycle_out_of_view_batches()
        app.processEvents()
        app.processEvents()
        drift = sb.value() - expected
        total_drift += abs(drift)
        print(f"  step {step}: 期望 {expected} 实际 {sb.value()} drift {drift:+d}")
    print(f"  累计 |drift| = {total_drift}")


if __name__ == "__main__":
    main()
