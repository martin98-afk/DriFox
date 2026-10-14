# -*- coding: utf-8 -*-
"""探针 AI：验证修复方案——删除前从待删子树摘焦点（用完即删）。

对比：
  组1 无保护 → 期望复现跳变
  组2 clearFocus 保护（模拟修复补丁）→ 期望原位保持
  组3 保护不影响无关场景：焦点在别处（输入框），删除远程卡片 → 期望原位保持（不误伤）
"""

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
N = 20


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
        "_max_rendered_cards": 40,
        "_min_rendered_cards": 6,
        "_unloaded_pids": [],
        "_pending_lazy_cards": [],
        "_lazy_batch_timer_active": False,
        "_history_load_threshold": 48,
        "_initial_scroll_to_bottom": True,
        "_suppress_scroll_sync_count": 0,
        "_visible_batch_start": 0,
        "_visible_batch_end": 8,
        "_batch_placeholders": {},
        "_batch_placeholder_overhead": {},
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
    for idx in range(N):
        card = MessageCard(role="assistant", parent=container)
        card.setFixedHeight(CARD_H)
        card._message_index = idx
        card._lazy_rendered = True
        card._content_just_loaded = False
        layout.addWidget(card)
        batch_cards.append([card])
        message_batch.append([{"role": "assistant", "content": f"batch {idx}"}])
    win._batch_cards = batch_cards
    win._message_batch = message_batch
    win._user_prefix_cache = [0] * (N + 1)

    scroll_area.show()
    container.show()
    for _ in range(5):
        app.processEvents()
    container.adjustSize()
    for _ in range(5):
        app.processEvents()
    return win


def focus_footer_btn(win, idx):
    card = win._batch_cards[idx][0]
    btns = [c for c in card.findChildren(QWidget) if c.focusPolicy() != Qt.NoFocus and c.isVisible()]
    if not btns:
        return None
    btns[0].setFocus(Qt.OtherFocusReason)
    for _ in range(3):
        app.processEvents()
    return btns[0]


def protect_focus(widgets):
    """拟议修复：删除前把焦点从待删子树中摘出。"""
    fw = QApplication.focusWidget()
    if fw is None:
        return
    for w in widgets:
        try:
            if w is fw or w.isAncestorOf(fw):
                fw.clearFocus()
                return
        except RuntimeError:
            continue


# ─── 组1：无保护 ───
win = build_window()
sb = win.chat_scroll_area.verticalScrollBar()
btn = focus_footer_btn(win, 0)
sb.setValue(10 * (CARD_H + 6))
for _ in range(4):
    app.processEvents()
v0 = sb.value()
win._recycle_out_of_view_batches()
for _ in range(6):
    app.processEvents()
v1 = sb.value()
p(f"[组1 无保护]  v0={v0} -> v1={v1}  drift={v1 - v0:+d}  {'JUMP（复现）' if abs(v1 - v0) > 30 else 'OK?'}")

# ─── 组2：保护（打桩 delete_widgets_from_layout 前置 clearFocus）───
p("")
win2 = build_window()
sb2 = win2.chat_scroll_area.verticalScrollBar()
btn2 = focus_footer_btn(win2, 0)

import app.widgets.ui_helpers as uh  # noqa: E402

_orig_del = uh.delete_widgets_from_layout


def _protected_del(widgets, layout, call_cleanup=True):
    protect_focus(widgets)  # ← 拟议修复
    return _orig_del(widgets, layout, call_cleanup)


uh.delete_widgets_from_layout = _protected_del
import app.main_widget as mw  # noqa: E402

if hasattr(mw, "delete_widgets_from_layout"):
    mw.delete_widgets_from_layout = _protected_del

sb2.setValue(10 * (CARD_H + 6))
for _ in range(4):
    app.processEvents()
v2 = sb2.value()
win2._recycle_out_of_view_batches()
for _ in range(6):
    app.processEvents()
v3 = sb2.value()
p(f"[组2 保护]    v0={v2} -> v1={v3}  drift={v3 - v2:+d}  {'PASS 原位' if abs(v3 - v2) <= 30 else 'FAIL'}")

# ─── 组3：焦点不在待删子树（误伤检查）───
p("")
win3 = build_window()
sb3 = win3.chat_scroll_area.verticalScrollBar()
# 焦点放到卡 0 的按钮，但删除的是「卡 10 之后」的批次（不在待删集）——不对，
# 回收总是删上方批次。改用：焦点在卡 5 的按钮（视口附近，不会被回收）
btn5 = focus_footer_btn(win3, 5)
sb3.setValue(12 * (CARD_H + 6))
for _ in range(4):
    app.processEvents()
v4 = sb3.value()
win3._recycle_out_of_view_batches()
for _ in range(6):
    app.processEvents()
v5 = sb3.value()
fw = QApplication.focusWidget()
p(f"[组3 焦点在保留卡] v0={v4} -> v1={v5}  drift={v5 - v4:+d}  焦点保持={fw is btn5 if btn5 else None}")

sys.stdout.flush()
os._exit(0)
