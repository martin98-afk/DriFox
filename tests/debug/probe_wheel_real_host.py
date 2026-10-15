# -*- coding: utf-8 -*-
"""探针（真实宿主链）：真实 OpenAIChatToolWindow 宿主 + 真实 MessageCard +
真实 viewer + 真实 OS 滚轮事件，复现「点击卡片正文后外层消息列表无法滚动」。

与 probe_wheel_armed.py 的差别：外层滚动区、卡片、viewer 全部走真实类，
宿主链真实存在（_route_wheel_to_chat_area 沿父链找 _parent 才拿得到），
滚轮走 user32.SendInput（真实 OS 事件，不经 Python 直调）。

场景：
  W1 未点击正文 → 外层滚动（基线）
  W2 点击正文   → 外层应滚动（当前 bug：0/5）
  W3 点击正文 + 灌内层可滚标志 → 外层不滚（折叠框体感，作对照）
"""

import ctypes
import os
import sys
import time
from unittest.mock import MagicMock

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PyQt5.QtCore import QEvent, QObject, QPoint, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
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

user32 = ctypes.windll.user32


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


def send_wheel(dy):
    inputs = (INPUT * 1)()
    inputs[0].type = 0
    inputs[0].mi = MOUSEINPUT(0, 0, ctypes.c_ulong(dy & 0xFFFFFFFF), 0x0800, 0, None)
    user32.SendInput(1, inputs, ctypes.sizeof(INPUT))


def pump(ms):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


VIEWPORT_H = 500
CARD_H = 260
N = 14


def build_window():
    from qfluentwidgets import SingleDirectionScrollArea

    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    for key, value in {
        "_is_destroyed": False,
        "_programmatic_scroll_depth": 0,
        "_is_virtual_recycling": False,
        "_loading_session": False,
        "_is_loading_history_batches": False,
        "_last_visible_card_ids": set(),
        "_current_assistant_card": None,
        "_rendered_card_count": 0,
        "_recycle_lru_call_count": 0,
        "_incremental_visible_batch_count": 8,
        "_virtual_scroll_buffer": 1,
        "_max_rendered_cards": 40,
        "_min_rendered_cards": 6,
        "_pending_lazy_cards": [],
        "_lazy_batch_timer_active": False,
        "_history_load_threshold": 48,
        "_initial_scroll_to_bottom": False,
        "_visible_batch_start": 0,
        "_visible_batch_end": 8,
        "_batch_placeholders": {},
        "_batch_placeholder_overhead": {},
        "_restore_queue": [],
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
        card._lazy_rendered = True
        layout.addWidget(card)
        cards.append(card)
    scroll_area.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
    scroll_area.move(60, 60)
    scroll_area.show()
    scroll_area.activateWindow()
    scroll_area.raise_()
    pump(1200)
    for card in cards:
        # 绕过可见性守卫：宿主是 __new__ 裸实例，_is_effectively_visible 走
        # window() 拿不到有效顶层窗口会无限推迟渲染
        card._is_effectively_visible = lambda: True
        card.ensure_rendered()
    pump(2500)
    for card in cards:
        if card.viewer is None:
            card._render_deferred = False
            card._lazy_rendered = False
            card.ensure_rendered()
    pump(2500)
    for card in cards:
        if card.viewer is not None:
            card.set_content([{"type": "text", "text": f"第 {card._message_index} 条消息正文。" * 30}])
    pump(2000)
    return win, scroll_area, container, cards


def click_delegate(view):
    fp = view.focusProxy()
    if fp is None:
        return False
    ev = QMouseEvent(QEvent.MouseButtonPress, QPoint(50, 50), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(fp, ev)
    pump(80)
    # 完整点击：无 release 时 Blink 保持鼠标捕获，后续 SendInput 滚轮路由失真
    ev2 = QMouseEvent(QEvent.MouseButtonRelease, QPoint(50, 50), Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
    QApplication.sendEvent(fp, ev2)
    pump(80)
    return True


def run_case(tag, armed, force_inner=None, ticks=5):
    print(f"=== {tag} ===")
    win, area, container, cards = build_window()
    card = cards[2]
    view = card.viewer
    if view is None:
        print("viewer 未创建，跳过")
        return None
    print(f"viewer={type(view).__name__} filter_target={'装' if view._wheel_filter_target else 'None'} "
          f"geom_valid={view._body_geom_valid} "
          f"max_scroll={view._inner_scroll_range()[1]} "
          f"inner_scrollable={view._inner_scrollable}")
    if force_inner is not None:
        view._on_body_geometry_reported(400, 0, 400)
        view._on_inner_scrollable_reported(force_inner)
        print(f"[探针] 灌内层标志 → {force_inner}")
    if armed:
        ok = click_delegate(view)
        print(f"点击正文 → armed={view._wheel_inner_armed} sent={ok}")
    tl = view.mapToGlobal(view.rect().topLeft())
    br = view.mapToGlobal(view.rect().bottomRight())
    cx, cy = (tl.x() + br.x()) // 2, (tl.y() + br.y()) // 2
    user32.SetCursorPos(ctypes.c_int(cx), ctypes.c_int(cy))
    pump(400)
    w = QApplication.widgetAt(cx, cy)
    print(f"鼠标({cx},{cy}) 命中 = {w.metaObject().className() if w else None}")
    vbar = area.verticalScrollBar()
    print(f"外层 range {vbar.minimum()}..{vbar.maximum()}")
    moved = 0
    for i in range(ticks):
        v0 = vbar.value()
        send_wheel(-120)
        pump(450)
        m = vbar.value() != v0
        moved += m
        print(f"  tick{i}: vbar {v0}->{vbar.value()} 外层动={m} armed={view._wheel_inner_armed}")
    print(f"结果: 外层滚动 {moved}/{ticks}")
    return moved


def main():
    w1 = run_case("W1 未点击正文（基线）", armed=False)
    w2 = run_case("W2 点击正文（复现）", armed=True)
    w3 = run_case("W3 点击正文 + 灌 inner_scrollable=True（对照）", armed=True, force_inner=True)
    print(f"SUMMARY W1={w1} W2={w2} W3={w3}")
    print("DONE-REAL-HOST")
    sys.exit(0)


if __name__ == "__main__":
    main()
