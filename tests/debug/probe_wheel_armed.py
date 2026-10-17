# -*- coding: utf-8 -*-
"""探针：武装态（点击过卡片正文）+ 卡内无滚轮 时，外层列表能否继续滚动。

场景：
  A 未点击 + body 无溢出        → 外层应滚动（既有体感基线）
  B 点击后(armed) + body 无溢出 → 复现 bug：外层不动
  C 点击后(armed) + body 溢出   → 卡内滚到底后外层接手（已知 OK，作对照）
  D 点击后(armed) + body 溢出 + 快滚自愈 → 连续 3 tick 后外层接手
"""

import ctypes
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
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

    def wheelEvent(self, event):
        self._wheel_hits += 1
        super().wheelEvent(event)


def pump(ms):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def build(body_max_h, lines=40, force_geom=None):
    area = QScrollArea()
    area.resize(600, 700)
    host_w = QWidget()
    lay = QVBoxLayout(host_w)
    card = DummyCard(DummyHost(area))
    card._wheel_hits = 0
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
    if force_geom is not None:
        # 探针专用：绕过真实 reportHeight 时序，直接灌入 body 几何，
        # 专门验证「几何有效但无溢出」这一支（B 场景的真实形态）
        sh, st, ch = force_geom
        view._on_body_geometry_reported(sh, st, ch)
        print(f"[探针] 注入几何 scrollHeight={sh} clientHeight={ch} → max_scroll={sh - ch}")
    return area, view, card


def click_delegate(view):
    """模拟用户点击卡片正文：直接给 delegate 发 MouseButtonPress（eventFilter 会收到）"""
    fp = view.focusProxy()
    if fp is None:
        return False
    ev = QMouseEvent(QEvent.MouseButtonPress, QPoint(50, 50), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(fp, ev)
    pump(50)
    return True


def run_case(tag, body_max_h, armed, dys=None, cx=380, cy=420, force_geom=None):
    print(f"=== {tag} ===")
    area, view, card = build(body_max_h, force_geom=force_geom)
    top, max_scroll = view._inner_scroll_range()
    print(f"body_geom_valid={view._body_geom_valid} state_judge(top={top}, max={max_scroll}) "
          f"filter_target={'装' if view._wheel_filter_target is not None else 'None'}")
    if armed:
        ok = click_delegate(view)
        print(f"点击正文 → armed={view._wheel_inner_armed} (sent={ok})")
    user32.SetCursorPos(ctypes.c_int(cx), ctypes.c_int(cy))
    pump(300)
    vbar = area.verticalScrollBar()
    print(f"外层range {vbar.minimum()}..{vbar.maximum()} card.wheelEvent命中基数={card._wheel_hits}")
    trace = []

    class Trace(QObject):
        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.Wheel:
                try:
                    name = obj.metaObject().className()
                except Exception:
                    name = "?"
                accepted = ev.isAccepted()
                trace.append(f"{name}(acc={accepted})")
            return False

    tr = Trace()
    app.installEventFilter(tr)
    moved = 0
    hits0 = card._wheel_hits
    for i, dy in enumerate(dys or [-120] * 5):
        v0 = vbar.value()
        trace.clear()
        send_wheel(dy)
        pump(400)
        m = vbar.value() != v0
        moved += m
        print(f"  tick{i} dy={dy}: vbar {v0}->{vbar.value()} 外层动={m} "
              f"armed={view._wheel_inner_armed} card.wheelEvent={card._wheel_hits - hits0} "
              f"chain={trace[:6]}")
    print(f"结果: 外层滚动 {moved}/{len(dys or [-120] * 5)}  card.wheelEvent累计={card._wheel_hits - hits0}")
    app.removeEventFilter(tr)
    area.hide()
    pump(200)
    return moved


def main():
    a = run_case("A 未点击 + body 无溢出（基线）", None, armed=False)
    b = run_case("B 点击后 + body 无溢出（复现）", None, armed=True)
    b2 = run_case("B2 点击后 + 几何有效但无溢出（真实形态）", None, armed=True,
                  force_geom=(400, 0, 400))
    c = run_case("C 点击后 + body 溢出（卡内优先）", 300, armed=True, dys=[-120] * 3,
                 force_geom=(1200, 0, 400))
    d = run_case("D 点击后 + body 溢出滚到底后继续滚（链式接管）", None, armed=True,
                 dys=[-120] * 20, force_geom=(1200, 0, 400))
    e = run_case("E 点击后 + body 溢出回滚到顶后继续滚（链式接管）", None, armed=True,
                 dys=[120] * 25, force_geom=(1200, 400, 400))
    print(f"SUMMARY A={a} B={b} B2={b2} C={c} D={d} E={e}")
    print("DONE-ARMED")
    sys.exit(0)


if __name__ == "__main__":
    main()
