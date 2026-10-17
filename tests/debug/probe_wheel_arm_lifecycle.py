# -*- coding: utf-8 -*-
"""探针：armed 态复位 + 黑洞路径定位。

场景：
  S1 点击正文 → 鼠标移出卡片 → 滚轮   （arm 是否复位？复位则外层应滚）
  S2 点击正文 → 原地滚轮，逐步灌几何   （几何未回传期是否黑洞）
  S3 点击正文 → 内层容器可滚但鼠标在正文区 → 滚轮（黑洞？）
"""

import ctypes
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget

from app.core.infra.webengine_profile import init_shared_web_profile
from app.widgets.card_viewers import CodeWebViewer

app = QApplication([])
init_shared_web_profile()
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


class DummyHost:
    def __init__(self, area):
        self.chat_scroll_area = area


class DummyCard(QWidget):
    def __init__(self, host):
        super().__init__()
        self._parent = host


_KEEP = []


def pump(ms):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def build(body_max_h, lines=40):
    area = QScrollArea()
    area.resize(600, 700)
    host_w = QWidget()
    lay = QVBoxLayout(host_w)
    card = DummyCard(DummyHost(area))
    card_lay = QVBoxLayout(card)
    view = CodeWebViewer()
    body_css = "margin:0" if body_max_h is None else f"margin:0;overflow-y:scroll;max-height:{body_max_h}px"
    html = ("<html><body style='" + body_css + "'>"
            + "".join(f"<p>line {i} some real text content here</p>" for i in range(lines))
            + "</body></html>")
    view.setHtml(html)
    view.setMinimumHeight(400)
    card_lay.addWidget(view)
    lay.addWidget(card)
    filler = QWidget()
    filler.setMinimumHeight(500)
    lay.addWidget(filler)
    lay.addStretch(1)
    area.setWidgetResizable(True)
    area.setWidget(host_w)
    area.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
    area.move(100, 100)
    area.show()
    area.activateWindow()
    area.raise_()
    pump(2500)
    _KEEP.append((area, host_w, card, view))
    return area, view, card


def click_delegate(view):
    fp = view.focusProxy()
    ev = QMouseEvent(QEvent.MouseButtonPress, QPoint(50, 50), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(fp, ev)
    pump(60)
    return fp


def state(view):
    return (f"armed={view._wheel_inner_armed} "
            f"geom_valid={view._body_geom_valid} "
            f"inner_scrollable={view._inner_scrollable} "
            f"max_scroll={view._inner_scroll_range()[1]}")


def wheel_ticks(area, view, n=5, dy=-120):
    vbar = area.verticalScrollBar()
    moved = 0
    for _ in range(n):
        v0 = vbar.value()
        send_wheel(dy)
        pump(450)
        moved += vbar.value() != v0
    return moved


def s1():
    print("=== S1 点击正文 → 鼠标移出卡片 → 滚轮 ===")
    area, view, card = build(None)
    click_delegate(view)
    print(f"点击后: {state(view)}")
    # 鼠标移到卡片外（窗口右上角外）
    user32.SetCursorPos(ctypes.c_int(900), ctypes.c_int(120))
    pump(600)
    print(f"移出后: {state(view)}")
    m = wheel_ticks(area, view)
    print(f"结果: 外层滚动 {m}/5")
    return m


def s2():
    print("=== S2 点击正文 → 几何逐步回传 → 滚轮 ===")
    area, view, card = build(None)
    click_delegate(view)
    print(f"点击后(无几何): {state(view)}")
    m0 = wheel_ticks(area, view, 2)
    print(f"几何未回传期: 外层 {m0}/2")
    view._on_body_geometry_reported(400, 0, 400)
    view._on_inner_scrollable_reported(False)
    print(f"灌几何后: {state(view)}")
    m1 = wheel_ticks(area, view, 3)
    print(f"几何回传后: 外层 {m1}/3")
    return m0, m1


def s3():
    print("=== S3 点击正文 → 内层可滚但鼠标在正文区 → 滚轮 ===")
    area, view, card = build(None)
    # 灌入"卡片内某处有可滚容器"的标志，但鼠标实际在正文
    view._on_body_geometry_reported(400, 0, 400)
    view._on_inner_scrollable_reported(True)
    click_delegate(view)
    print(f"点击后: {state(view)}")
    m = wheel_ticks(area, view)
    print(f"结果: 外层滚动 {m}/5")
    return m


def main():
    a = s1()
    b, c = s2()
    d = s3()
    print(f"SUMMARY S1(移出后)={a} S2(几何前/后)={b}/{c} S3(内层可滚)={d}")
    print("DONE-ARM")
    sys.exit(0)


if __name__ == "__main__":
    main()
