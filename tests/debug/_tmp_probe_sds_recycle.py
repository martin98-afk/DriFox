# -*- coding: utf-8 -*-
"""探针 X：真实 SingleDirectionScrollArea + 真 MessageCard，复现焦点引发的回收跳变（用完即删）。"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock  # noqa: E402

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication([])

from app.main_widget import OpenAIChatToolWindow  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

VIEWPORT_H = 400
CARD_H = 300
BATCH = 30


def p(*a):
    print(*a)
    sys.stdout.flush()


def build_window():
    from qfluentwidgets import SingleDirectionScrollArea

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
    win._is_widget_alive = lambda w: w is not None
    win._get_load_msg_extras_fn = lambda: None
    win._current_session_id_for_extras = lambda: "s1"
    win._get_user_round_index_for_batch_index = MagicMock(return_value=0)
    win._update_card_diff_stats = MagicMock()
    win._restore_meta_from_batch = MagicMock()
    win._create_message_widget = MagicMock(return_value=None)
    win._sync_single_card_width = MagicMock()

    scroll_area = SingleDirectionScrollArea()
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(6, 6, 6, 6)
    layout.setSpacing(6)
    layout.setAlignment(Qt.AlignBottom)
    scroll_area.setWidget(container)
    scroll_area.setWidgetResizable(True)
    scroll_area.resize(500, VIEWPORT_H)
    win.chat_scroll_area = scroll_area
    win.chat_container = container
    win.chat_layout = layout

    batch_cards, message_batch = [], []
    for idx in range(BATCH):
        card = MessageCard(role="assistant", parent=container)
        card.setFixedHeight(CARD_H)
        card._message_index = idx
        card._lazy_rendered = True
        layout.addWidget(card)
        batch_cards.append([card])
        message_batch.append([{"role": "assistant", "content": f"batch {idx}"}])
    # 让卡片里确实有可见按钮（对齐「用户点过卡内按钮」场景）
    container.show()
    for _ in range(5):
        app.processEvents()
    win._batch_cards = batch_cards
    win._message_batch = message_batch
    win._batch_placeholders = {}
    win._user_prefix_cache = [0] * (BATCH + 1)

    scroll_area.show()
    for _ in range(3):
        app.processEvents()
    container.adjustSize()
    for _ in range(3):
        app.processEvents()
    return win


def focus_button_in_card(win, idx):
    card = win._batch_cards[idx][0]
    focusables = [c for c in card.findChildren(QWidget) if c.focusPolicy() != Qt.NoFocus]
    if not focusables:
        return None
    focusables[0].setFocus(Qt.OtherFocusReason)
    for _ in range(2):
        app.processEvents()
    fw_now = QApplication.focusWidget()
    p(f'    setFocus 生效? {fw_now is focusables[0]}  实际 focusWidget={type(fw_now).__name__ if fw_now else None}')
    p(f'    focusables[0].isVisible()={focusables[0].isVisible()}')
    return focusables[0]


# ─── 场景 1：无保护（复现）───
win = build_window()
sb = win.chat_scroll_area.verticalScrollBar()
p(f"scrollbar type = {type(sb).__name__}")
fw = focus_button_in_card(win, 0)
p(f"焦点控件 = {type(fw).__name__}（卡 0）")

sb.setValue(15 * (CARD_H + 6))
for _ in range(3):
    app.processEvents()
v0 = sb.value()
p(f"中段阅读: value={v0} max={sb.maximum()}")

win._recycle_out_of_view_batches()
for _ in range(5):
    app.processEvents()
v1 = sb.value()
p(f"[场景1 无保护] 回收后 value={v1}  drift={v1 - v0:+d}  {'FAIL 跳变' if abs(v1 - v0) > 30 else 'OK'}")

# ─── 场景 2：保护（clearFocus）───
p("")
win2 = build_window()
sb2 = win2.chat_scroll_area.verticalScrollBar()
fw2 = focus_button_in_card(win2, 0)
sb2.setValue(15 * (CARD_H + 6))
for _ in range(3):
    app.processEvents()
v2 = sb2.value()

# 保护：焦点在聊天区子树内 → clearFocus
fw_now = QApplication.focusWidget()
if fw_now is not None and win2.chat_scroll_area.isAncestorOf(fw_now):
    fw_now.clearFocus()
    for _ in range(2):
        app.processEvents()

win2._recycle_out_of_view_batches()
for _ in range(5):
    app.processEvents()
v3 = sb2.value()
p(f"[场景2 保护]  回收后 value={v3}  drift={v3 - v2:+d}  {'PASS 原位保持' if abs(v3 - v2) <= 30 else 'FAIL'}")

sys.stdout.flush()
os._exit(0)
