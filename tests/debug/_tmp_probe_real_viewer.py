# -*- coding: utf-8 -*-
"""探针 V：真实 CodeWebViewer 卡片——焦点在 viewer/delegate 上时回收是否跳变（用完即删）。

模拟用户「点击过卡片正文」（QWebEngineView 获得焦点）后再滚动回收。
"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication([])

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402

try:
    init_shared_web_profile()
except Exception:  # noqa: BLE001
    pass

from app.main_widget import OpenAIChatToolWindow  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

VIEWPORT_H = 400
CARD_H = 300
BATCH = 30


def p(*a):
    print(*a)
    sys.stdout.flush()


def build_window():
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

    scroll_area = QScrollArea()
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
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


win = build_window()
sb = win.chat_scroll_area.verticalScrollBar()
lay = win.chat_layout
container = win.chat_container

# 让卡 0 渲染真实 viewer
card0 = win._batch_cards[0][0]
card0._content_data = {"role": "assistant", "content": "hello world"}
card0.ensure_rendered()
for _ in range(10):
    app.processEvents()
p(f"card0.viewer={type(card0.viewer).__name__} focusProxy={card0.viewer.focusProxy()}")

# 模拟「用户点击过卡片正文」→ 焦点在 viewer 或其 delegate
fp = card0.viewer.focusProxy()
if fp is not None:
    fp.setFocus(Qt.OtherFocusReason)
else:
    card0.viewer.setFocus(Qt.OtherFocusReason)
for _ in range(2):
    app.processEvents()
p(f"焦点设置后: focusWidget={type(QApplication.focusWidget()).__name__} in card0 subtree={card0.isAncestorOf(QApplication.focusWidget()) if QApplication.focusWidget() else None}")

sb.setValue(sb.maximum())
for _ in range(2):
    app.processEvents()
v0 = sb.value()
p(f"滚到底: value={v0} max={sb.maximum()}")

# 真实 detach 路径：让 _try_detach_card_viewer 用真实实现
win._try_detach_card_viewer = lambda card: card.detach_viewer()
from app.main_widget import OpenAIChatToolWindow as W  # noqa: E402

win._try_detach_card_viewer = W._try_detach_card_viewer.__get__(win, W)

# 只回收 card0（上方），观察 detach 与 delete 两步
p("\n=== step1: detach_viewer (viewer.hide) ===")
win._try_detach_card_viewer(card0)
for _ in range(2):
    app.processEvents()
p(f"after detach: value={sb.value()} 焦点={type(QApplication.focusWidget()).__name__ if QApplication.focusWidget() else None}")

from app.widgets.ui_helpers import delete_widgets_from_layout  # noqa: E402

p("\n=== step2: delete card0 (card.hide) ===")
delete_widgets_from_layout([card0], lay)
for _ in range(3):
    app.processEvents()
v1 = sb.value()
p(f"after delete: value={v1} drift={v1 - v0:+d} {'JUMP' if abs(v1 - v0) > 2 else 'OK'}")

sys.stdout.flush()
os._exit(0)
