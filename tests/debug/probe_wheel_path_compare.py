# -*- coding: utf-8 -*-
"""探针：同一组状态下，两条派发路径的行为差异。

单测路径：view.eventFilter(fp, ev)（Python 直接调用）
真实路径：user32.SendInput → Qt 事件队列 → fp 的 eventFilter

若两条不一致，差异必在「Qt 真实派发是否经过 fp 的 eventFilter」。
"""

import ctypes
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt5.QtGui import QMouseEvent, QWheelEvent
from PyQt5.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget

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


def build():
    area = QScrollArea()
    area.resize(600, 700)
    host_w = QWidget()
    lay = QVBoxLayout(host_w)
    card = DummyCard(DummyHost(area))
    card_lay = QVBoxLayout(card)
    view = CodeWebViewer()
    view.setHtml("<html><body style='margin:0'>"
                 + "".join(f"<p>line {i} real text content here</p>" for i in range(40))
                 + "</body></html>")
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


def arm(view):
    fp = view.focusProxy()
    ev = QMouseEvent(QEvent.MouseButtonPress, QPoint(50, 50), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(fp, ev)
    pump(80)


def main():
    print("=== P1 单测路径（Python 直调 eventFilter）===")
    area, view, card = build()
    arm(view)
    view._on_body_geometry_reported(400, 0, 400)
    view._on_inner_scrollable_reported(False)
    print(f"state: armed={view._wheel_inner_armed} geom_valid={view._body_geom_valid} "
          f"inner_scrollable={view._inner_scrollable}")
    fp = view.focusProxy()
    ev = QWheelEvent(QPointF(20, 20), QPointF(20, 20), QPoint(0, 0), QPoint(0, -120),
                     Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    ret = view.eventFilter(fp, ev)
    vbar = area.verticalScrollBar()
    print(f"eventFilter 返回={ret} 外层 vbar={vbar.value()}")

    print()
    print("=== P2 真实路径（SendInput）===")
    area2, view2, card2 = build()
    arm(view2)
    view2._on_body_geometry_reported(400, 0, 400)
    view2._on_inner_scrollable_reported(False)
    print(f"state: armed={view2._wheel_inner_armed} geom_valid={view2._body_geom_valid} "
          f"inner_scrollable={view2._inner_scrollable}")
    v0 = vbar2 = area2.verticalScrollBar().value()
    send_wheel(-120)
    pump(500)
    print(f"SendInput 后 外层 vbar {v0}->{area2.verticalScrollBar().value()}")

    print()
    print("=== P3 同一 viewer 上真实路径（不重建）===")
    v0 = vbar.value()
    print(f"SendInput 前 armed={view._wheel_inner_armed} vbar={v0}")
    user32.SetCursorPos(ctypes.c_int(380), ctypes.c_int(420))
    pump(300)
    send_wheel(-120)
    pump(500)
    print(f"SendInput 后 armed={view._wheel_inner_armed} vbar {v0}->{vbar.value()}")
    print("DONE-PATH")
    sys.exit(0)


if __name__ == "__main__":
    main()
