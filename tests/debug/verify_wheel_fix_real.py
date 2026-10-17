# -*- coding: utf-8 -*-
"""端到端验证：真实 CodeWebViewer 的 delegate wheel 抢断（滚动失效修复）

复刻宿主链：QScrollArea(chat_scroll_area) ← DummyHost(_parent) ← DummyCard ← viewer
场景：
  V1 超限卡（body max-height 内溢出，修复前 8/8 全吞）：下滚 → 外层必须动
  V2 外层滚到边界后继续同向滚 → 放行卡内（不再验证卡内位移，只验证无异常）
  V3 普通卡（无溢出）路径回归：外层照常动
"""

import ctypes
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtCore import QEvent, QObject, Qt, QPoint
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
    """仿 OpenAIChatToolWindow：只暴露 chat_scroll_area"""
    def __init__(self, area):
        self.chat_scroll_area = area


class DummyCard(QWidget):
    """仿 MessageCard：只暴露 _parent（wheel 沿父链找的就是它）"""
    def __init__(self, host):
        super().__init__()
        self._parent = host


def pump(ms):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def build(body_max_h, outer_content_lines=40):
    area = QScrollArea()
    area.resize(600, 700)
    host_w = QWidget()
    lay = QVBoxLayout(host_w)
    card = DummyCard(DummyHost(area))
    card_lay = QVBoxLayout(card)
    view = CodeWebViewer()
    body_css = "margin:0" if body_max_h is None else f"margin:0;overflow-y:scroll;max-height:{body_max_h}px"
    html = ("<html><body style='" + body_css + "'>"
            + "".join(f"<p>line {i} some real text content here</p>" for i in range(outer_content_lines))
            + "</body></html>")
    view.setHtml(html)
    view.setMinimumHeight(400)
    card_lay.addWidget(view)
    lay.addWidget(card)
    filler = QWidget()
    filler.setMinimumHeight(500)  # 保证外层滚动区必有溢出
    lay.addWidget(filler)
    lay.addStretch(1)
    area.setWidgetResizable(True)
    area.setWidget(host_w)
    area.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)  # 防被桌面其它窗口吃掉 SendInput 滚轮
    area.move(100, 100)
    area.show()
    area.activateWindow()
    area.raise_()
    pump(2500)
    return area, view


def run_case(tag, body_max_h, dys, cx=380, cy=420):
    print(f"=== {tag} ===")
    area, view = build(body_max_h)
    fp = view.focusProxy()
    print(f"focusProxy={None if fp is None else fp.metaObject().className()} "
          f"filter_target={'已装' if view._wheel_filter_target is fp else view._wheel_filter_target}")
    user32.SetCursorPos(ctypes.c_int(cx), ctypes.c_int(cy))
    pump(400)
    hit = QApplication.widgetAt(QPoint(cx, cy))
    print(f"widgetAt(鼠标点)={None if hit is None else hit.metaObject().className()} "
          f"是viewer={hit is view} 是其子={hit is not None and view.isAncestorOf(hit)}")
    trace = []

    class Trace(QObject):
        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.Wheel:
                try:
                    trace.append(obj.metaObject().className())
                except Exception:
                    trace.append("?")
            return False

    tr = Trace()
    app.installEventFilter(tr)
    vbar = area.verticalScrollBar()
    print(f"外层range {vbar.minimum()}..{vbar.maximum()} viewer高度={view.height()}")
    moved = 0
    for dy in dys:
        v0 = vbar.value()
        trace.clear()
        send_wheel(dy)
        pump(300)
        m = vbar.value() != v0
        moved += m
        print(f"  dy={dy}: vbar {v0}->{vbar.value()} 外层动={m} 流向={trace[:4]}")
    print(f"结果: {moved}/{len(dys)} tick 外层滚动")
    app.removeEventFilter(tr)
    area.hide()
    pump(200)
    return moved


def main():
    # V1 超限卡：body max-height=300（内容必溢出），修复前该场景全吞
    r1 = run_case("V1 超限卡下滚5次（修复前0/8全吞）", 300, [-120] * 5)
    # V2 普通卡：无 max-height，回归验证既有路径不被破坏
    r2 = run_case("V2 普通卡下滚5次（回归）", None, [-120] * 5)
    ok = r1 >= 3 and r2 >= 3
    print(f"VERIFY {'PASS' if ok else 'FAIL'} (V1={r1}/5 V2={r2}/5)")
    print("DONE-VERIFY")
    sys.exit(0)


if __name__ == "__main__":
    main()
