# -*- coding: utf-8 -*-
"""探针3：修正版——外层有真实溢出，测「上抛后转发外层」通路 + 吞吐稳定性

场景（窗口激活、外层 vbar 必可动）：
  S1 普通卡（body 无溢出）：连续 8 次 tick，每次 vbar 必须动。统计上抛率。
  S2 超限卡（body max-height=300 内溢出）：同 tick 序列，看卡内滚 vs 外层滚。
  S3 S2 卡内 body 滚到底后继续滚：外层接手吗（chaining 断链）？
"""

import ctypes
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtCore import QObject, QEvent, Qt
from PySide6.QtWidgets import (QApplication, QLabel, QScrollArea, QVBoxLayout,
                             QWidget)

from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402

app = QApplication([])
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


class V(QWebEngineView):
    def __init__(self):
        super().__init__()
        self.log = []
        self.setFixedHeight(400)

    def wheelEvent(self, event):
        self.log.append(event.angleDelta().y())
        # 复刻 CodeWebViewer 转发逻辑
        area = self.window().findChild(QScrollArea)
        if area is not None:
            vbar = area.verticalScrollBar()
            dy = event.angleDelta().y()
            if vbar and vbar.minimum() != vbar.maximum() and dy != 0:
                at_bound = (dy > 0 and vbar.value() <= vbar.minimum()) or (
                    dy < 0 and vbar.value() >= vbar.maximum())
                if not at_bound:
                    area.wheelEvent(event)
                    event.accept()
                    return
        super().wheelEvent(event)


LONG_HTML = ("<html><body style='margin:0'>"
             + "".join(f"<p>line {i} padded content for scroll range</p>" for i in range(400))
             + "</body></html>")
SHORT_HTML = "<html><body style='margin:0'><p>short body</p></body></html>"

filt_logs = []


class Filt(QObject):
    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Wheel:
            filt_logs.append(obj.metaObject().className())
        return False


def pump(ms):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def scenario(name, html, body_max_h, dys, activate_first=True):
    print(f"=== {name} ===")
    area = QScrollArea()
    area.resize(500, 600)
    host = QWidget()
    lay = QVBoxLayout(host)
    # 顶部 filler：确保外层 vbar 一定可动
    filler = QLabel("FILLER\n" * 12)
    lay.addWidget(filler)
    view = V()
    body_css = "margin:0" if body_max_h is None else f"margin:0;overflow-y:scroll;max-height:{body_max_h}px"
    view.setHtml(html.replace("<body style='margin:0'>", f"<body style='{body_css}'>"))
    lay.addWidget(view)
    lay.addStretch(1)
    area.setWidgetResizable(True)
    area.setWidget(host)
    area.move(100, 100)
    area.show()
    if activate_first:
        area.activateWindow()
        area.raise_()
    pump(2000)

    filt_logs.clear()
    rf = Filt()
    fp = view.focusProxy()
    if fp is not None:
        fp.installEventFilter(rf)
    view.installEventFilter(rf)

    user32.SetCursorPos(ctypes.c_int(300), ctypes.c_int(450))  # 落在 view 上（filler 12行约 300px 高）
    pump(400)

    vbar = area.verticalScrollBar()
    print(f"外层range: {vbar.minimum()}..{vbar.maximum()} focusProxy={'有' if fp is not None else '无'}")

    # 卡内 body scrollTop 旁听：用 js 高度回报不可用（禁 runJavaScript），改从旁听事件推断
    results = []
    for dy in dys:
        v0 = vbar.value()
        n_view = len(view.log)
        filt_logs.clear()
        send_wheel(dy)
        pump(300)
        v1 = vbar.value()
        called = len(view.log) > n_view
        results.append((dy, v0, v1, called, len(filt_logs)))
        mark = "外层动" if v1 != v0 else ("view转发失败?" if called else "全吞")
        print(f"  dy={dy}: vbar {v0}->{v1} view.wheelEvent被调={called} delegate侧Wheel事件数={len(filt_logs)} => {mark}")
    swallowed = sum(1 for r in results if r[2] == r[1] and not r[3])
    print(f"  汇总: {len(results)} tick, 全吞={swallowed}, 上抛率={sum(1 for r in results if r[3])}/{len(results)}")
    area.hide()
    pump(200)
    return results


def main():
    app.processEvents()
    print("--- S1 普通卡（body无溢出）向上滚 8 tick ---")
    r1 = scenario("S1", SHORT_HTML, None, [-120] * 8)
    print("--- S2 超限卡（max-height=300）向上滚 8 tick（外层有余量时本应滚外层） ---")
    r2 = scenario("S2", LONG_HTML, 300, [-120] * 8)
    print("--- S3 超限卡先下滚到顶（滚到底）再上滚 4 tick：看 chaining ---")
    scenario("S3", LONG_HTML, 300, [120] * 8 + [-120] * 4)
    print("DONE-PROBE3")
    sys.exit(0)


if __name__ == "__main__":
    main()
