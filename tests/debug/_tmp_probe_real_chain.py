# -*- coding: utf-8 -*-
"""探针 AA：真实链路——焦点在卡内（WebEngine delegate）时回收卡片的跳变复现（用完即删）。

对齐用户场景：
  1. 用户点击过某张卡片正文（WebEngine 获得焦点，focusProxy=delegate）
  2. 用户滚轮滚到中段阅读
  3. 虚拟滚动回收视口外批次（含焦点卡）
  4. 观察 value 漂移 + 焦点演化（detach / hide / delete 各自影响）
"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock  # noqa: E402

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

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
        "_max_rendered_cards": 30,
        "_min_rendered_cards": 6,
        "_unloaded_pids": [],
        "_pending_lazy_cards": [],
        "_lazy_batch_timer_active": False,
        "_history_load_threshold": 48,
        "_initial_scroll_to_bottom": True,
        "_suppress_scroll_sync_count": 0,
        "_visible_batch_start": 0,
        "_visible_batch_end": N,
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
        card._lazy_rendered = False  # 待渲染
        layout.addWidget(card)
        batch_cards.append([card])
        message_batch.append([{"role": "assistant", "content": f"batch {idx} 这是第 {idx} 条消息的内容。"}])
    win._batch_cards = batch_cards
    win._message_batch = message_batch
    win._batch_placeholders = {}
    win._user_prefix_cache = [0] * (N + 1)
    scroll_area.show()
    container.show()
    for _ in range(3):
        app.processEvents()
    return win


win = build_window()
sb = win.chat_scroll_area.verticalScrollBar()
container = win.chat_container

# 渲染卡 0（点击过正文的目标卡）
card0 = win._batch_cards[0][0]
card0._content_data = {"role": "assistant", "content": "这是被我点过的卡片。"}
card0.ensure_rendered()
p(f"card0._lazy_rendered={card0._lazy_rendered} viewer={card0.viewer is not None} deferred={getattr(card0, '_render_deferred', None)}")

# 让回收走「detach 失败」路径（模拟 user 卡 / 池回绝）：
# 直接 patch _try_detach_card_viewer 返回 False
win._try_detach_card_viewer = lambda card: False


def after_load():
    v = card0.viewer
    if v is None:
        p("!! card0 viewer 仍为 None，放弃")
        sys.stdout.flush()
        os._exit(3)
    fp = v.focusProxy()
    p(f"viewer={type(v).__name__} focusProxy={type(fp).__name__ if fp else None}")

    # 模拟「点击过卡片正文」：焦点给 delegate
    if fp is not None:
        fp.setFocus(Qt.OtherFocusReason)
    else:
        v.setFocus(Qt.OtherFocusReason)
    for _ in range(3):
        app.processEvents()
    fw = QApplication.focusWidget()
    p(f"焦点设置后：focusWidget={type(fw).__name__ if fw else None}  在 card0 子树内={card0.isAncestorOf(fw) if fw else None}")

    # 滚到中段（卡0 回到视口上方）
    sb.setValue(10 * (CARD_H + 6))
    for _ in range(3):
        app.processEvents()
    v0 = sb.value()
    p(f"中段: value={v0} max={sb.maximum()}")

    # 打桩观测 detach 与 delete 各自是否引起 value 变化
    _orig_detach = MessageCard.detach_viewer

    def _detach(self):
        before = sb.value()
        r = _orig_detach(self)
        for _ in range(2):
            app.processEvents()
        p(f"    [detach_viewer] {before} -> {sb.value()}  ({'跳' if abs(sb.value() - before) > 2 else '稳'}) 焦点={type(QApplication.focusWidget()).__name__ if QApplication.focusWidget() else None}")
        return r

    MessageCard.detach_viewer = _detach

    p("\n=== 触发回收（无保护）===")
    win._recycle_out_of_view_batches()
    for _ in range(5):
        app.processEvents()
    v1 = sb.value()
    p(f"[无保护] 回收后 value={v1}  drift={v1 - v0:+d}  {'FAIL 跳变' if abs(v1 - v0) > 30 else 'OK'}")
    p(f"    回收批次={[i for i, b in enumerate(win._batch_cards) if b is None]}")
    p(f"    焦点 now={type(QApplication.focusWidget()).__name__ if QApplication.focusWidget() else None}")

    sys.stdout.flush()
    os._exit(0)


if card0.viewer is not None:
    after_load()
else:
    QTimer.singleShot(2000, after_load)
    app.exec_()
