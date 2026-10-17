# -*- coding: utf-8 -*-
"""探针（真实宿主链 + 合成 wheel）：点击正文后外层列表是否还能滚动。

SendInput 探针在当前桌面环境不可靠（widgetAt 命中 None、渲染未完成）。
本探针改用**合成 QWheelEvent 直接派发到 delegate**（focusProxy），
即真实滚轮事件的第一接收者，绕开 OS 焦点依赖。
合成事件仍会走 Chromium 的 Qt 事件入口（eventFilter → Blink），
已由 probe_wheel_armed_inner 验证能区分内外归属。

场景：
  X1 未点击正文            → 外层滚（基线）
  X2 点击正文              → 外层滚（复现目标）
  X3 点击正文 + 内层可滚   → 内层滚、外层不滚（折叠框体感，对照）
  X4 点击正文 + body 溢出   → 卡内滚（对照）
"""

import os
import sys
import time
from unittest.mock import MagicMock

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent, QWheelEvent  # noqa: E402
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


def pump(ms):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


VIEWPORT_H = 500
N = 14


def build_window():
    from qfluentwidgets import SingleDirectionScrollArea

    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    for key, value in {
        "_is_destroyed": False, "_programmatic_scroll_depth": 0, "_is_virtual_recycling": False,
        "_loading_session": False, "_is_loading_history_batches": False,
        "_last_visible_card_ids": set(), "_current_assistant_card": None,
        "_rendered_card_count": 0, "_recycle_lru_call_count": 0,
        "_incremental_visible_batch_count": 8, "_virtual_scroll_buffer": 1,
        "_max_rendered_cards": 40, "_min_rendered_cards": 6, "_pending_lazy_cards": [],
        "_lazy_batch_timer_active": False, "_history_load_threshold": 48,
        "_initial_scroll_to_bottom": False, "_visible_batch_start": 0, "_visible_batch_end": 8,
        "_batch_placeholders": {}, "_batch_placeholder_overhead": {}, "_restore_queue": [],
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
    scroll_area.resize(600, VIEWPORT_H)
    win.chat_scroll_area = scroll_area
    win.chat_container = container
    win.chat_layout = layout

    cards = []
    for idx in range(N):
        card = MessageCard(role="assistant", parent=container)
        card._parent = win
        card._message_index = idx
        card._is_effectively_visible = lambda: True
        layout.addWidget(card)
        cards.append(card)
    scroll_area.show()
    pump(800)
    for card in cards:
        card.ensure_rendered()
    pump(1500)
    for card in cards:
        if card.viewer is not None:
            card.set_content([{"type": "text", "text": f"第 {card._message_index} 条消息正文。" * 30}])
    pump(2500)
    return win, scroll_area, container, cards


def click_delegate(view):
    fp = view.focusProxy()
    if fp is None:
        return False
    ev = QMouseEvent(QEvent.MouseButtonPress, QPoint(50, 50), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(fp, ev)
    pump(80)
    return True


def wheel(view, dy):
    fp = view.focusProxy()
    ev = QWheelEvent(QPointF(20, 20), QPointF(20, 20), QPoint(0, 0), QPoint(0, dy),
                     Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    consumed = view.eventFilter(fp, ev)
    if not consumed:
        QApplication.sendEvent(fp, ev)
    pump(400)
    return consumed


def run_case(tag, armed, geom=None, inner=None, ticks=5):
    print(f"=== {tag} ===")
    win, area, container, cards = build_window()
    card = cards[2]
    view = card.viewer
    if view is None:
        print("viewer 未创建，跳过")
        return None
    if geom is not None:
        view._on_body_geometry_reported(*geom)
    if inner is not None:
        view._on_inner_scrollable_reported(inner)
    print(f"state: geom_valid={view._body_geom_valid} max_scroll={view._inner_scroll_range()[1]} "
          f"inner_scrollable={view._inner_scrollable}")
    if armed:
        click_delegate(view)
        print(f"点击正文 → armed={view._wheel_inner_armed}")
    vbar = area.verticalScrollBar()
    print(f"外层 range {vbar.minimum()}..{vbar.maximum()}")
    moved = 0
    for i in range(ticks):
        v0 = vbar.value()
        c = wheel(view, -120)
        m = vbar.value() != v0
        moved += m
        top, mx = view._inner_scroll_range()
        print(f"  tick{i}: filter->{c} vbar {v0}->{vbar.value()} 外层动={m} "
              f"body(top={top},max={mx})")
    print(f"结果: 外层滚动 {moved}/{ticks}")
    return moved


def main():
    x1 = run_case("X1 未点击正文（基线）", armed=False)
    x2 = run_case("X2 点击正文（复现目标）", armed=True)
    x3 = run_case("X3 点击正文 + 内层可滚（折叠框对照）", armed=True,
                  geom=(400, 0, 400), inner=True)
    x4 = run_case("X4 点击正文 + body 溢出（卡内优先对照）", armed=True,
                  geom=(1200, 0, 400))
    print(f"SUMMARY X1={x1} X2={x2} X3={x3} X4={x4}")
    print("DONE-REAL-HOST-SYNTH")
    sys.exit(0)


if __name__ == "__main__":
    main()
