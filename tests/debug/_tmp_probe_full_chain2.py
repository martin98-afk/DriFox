# -*- coding: utf-8 -*-
"""探针 AH：完整回收链路 + 焦点在卡内页脚按钮（viewer 之外）是否会跳变（用完即删）。

这是最接近真实的复现尝试：
- assistant 卡有真实 viewer（detach 会摘掉它，但不影响页脚按钮）
- 焦点在页脚 TransparentToolButton（hover 显现的复制/重试按钮——用户点过）
- 走完整回收链路：_recycle_out_of_view_batches
"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock  # noqa: E402

from PyQt5.QtCore import Qt, QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

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
N = 20


def p(*a):
    print(*a)
    sys.stdout.flush()


def build_window(*, cards_with_viewer=4):
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


win = build_window()
sb = win.chat_scroll_area.verticalScrollBar()
lay = win.chat_layout

# 找卡0 内的页脚按钮（Viewer 外的可聚焦控件）
card0 = win._batch_cards[0][0]
all_focusables = [c for c in card0.findChildren(QWidget) if c.focusPolicy() != Qt.NoFocus]
p(f"卡0 可聚焦控件: {[(type(c).__name__, c.isVisible()) for c in all_focusables]}")

# 挑一个可见的页脚按钮
btn = None
for c in all_focusables:
    if c.isVisible():
        btn = c
        break

if btn is None:
    # 强制显示一个
    if all_focusables:
        btn = all_focusables[0]
        btn.show()
        for _ in range(3):
            app.processEvents()

p(f"选中按钮: {type(btn).__name__} visible={btn.isVisible()}")
btn.setFocus(Qt.OtherFocusReason)
for _ in range(3):
    app.processEvents()
fw = QApplication.focusWidget()
p(f"焦点: {type(fw).__name__} 在卡0子树={card0.isAncestorOf(fw) if fw else None}")

# 用户滚到中段
sb.setValue(10 * (CARD_H + 6))
for _ in range(4):
    app.processEvents()
v0 = sb.value()
p(f"\n中段: value={v0} max={sb.maximum()}")

# 打桩：观察 detach 对焦点的影响
_orig_detach = MessageCard.detach_viewer


def _detach(self):
    before = sb.value()
    fw_before = QApplication.focusWidget()
    r = _orig_detach(self)
    for _ in range(2):
        app.processEvents()
    fw_after = QApplication.focusWidget()
    p(
        f"    [detach] {before}->{sb.value()} 焦点 {type(fw_before).__name__ if fw_before else None}"
        f" -> {type(fw_after).__name__ if fw_after else None}"
    )
    return r


MessageCard.detach_viewer = _detach

p("\n=== 触发回收 ===")
win._recycle_out_of_view_batches()
for _ in range(6):
    app.processEvents()
v1 = sb.value()
p(f"回收后: value={v1} drift={v1 - v0:+d}  {'FAIL 跳变' if abs(v1 - v0) > 30 else 'OK 原位'}")
p(f"回收批次={[i for i, b in enumerate(win._batch_cards) if b is None]}")
fw = QApplication.focusWidget()
p(f"焦点 now={type(fw).__name__ if fw else None}")

sys.stdout.flush()
os._exit(0)
