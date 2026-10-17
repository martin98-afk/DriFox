# -*- coding: utf-8 -*-
"""探针：tool-content 溢出的卡片，武装态滚轮归属。

不依赖鼠标位置 / 窗口焦点（SendInput 探针在桌面被抢焦时结果不可信），
直接给 delegate（focusProxy）派 QWheelEvent，走真实 eventFilter 路径。
真实滚轮行为由 tests/debug/probe_wheel_armed.py 的 A/B/B2/C/D/E 覆盖。

场景：
  L armed + tool-content 可滚 + body 不溢出 → tool-content 滚，外层不动
  M 未点击 + 同几何                          → 外层滚（保持外层优先基线）
  N armed + 内外都不可滚                     → 外层滚（B2 回归）
  O armed + body 溢出                        → body 滚（C 回归）
"""

import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget

from app.core.infra.webengine_profile import init_shared_web_profile
from app.widgets.card_viewers import CodeWebViewer

app = QApplication([])
init_shared_web_profile()
_KEEPALIVE = []

READ_JS = (
    "(function(){var t=document.getElementById('tool-content');"
    "return t?String(t.scrollTop):'none';})()"
)


class DummyHost:
    def __init__(self, area):
        self.chat_scroll_area = area


class DummyCard(QWidget):
    def __init__(self, host):
        super().__init__()
        self._parent = host
        self._wheel_hits = 0

    def wheelEvent(self, event):
        self._wheel_hits += 1
        super().wheelEvent(event)


class FakeScrollBar:
    def __init__(self, value, minimum, maximum):
        self._v, self._min, self._max = value, minimum, maximum

    def value(self):
        return self._v

    def minimum(self):
        return self._min

    def maximum(self):
        return self._max


class FakeArea:
    """桩滚动区：记录被转发了几次"""

    def __init__(self, value=100, minimum=0, maximum=500):
        self._got = 0
        self.vbar = FakeScrollBar(value, minimum, maximum)

    def verticalScrollBar(self):
        return self.vbar

    def wheelEvent(self, ev):
        self._got += 1


def pump(ms):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def build(body_max_h, tc_lines, force_geom, force_inner):
    area = QScrollArea()
    area.resize(600, 700)
    host_w = QWidget()
    lay = QVBoxLayout(host_w)
    card = DummyCard(DummyHost(area))
    card_lay = QVBoxLayout(card)
    view = CodeWebViewer()
    body_css = "margin:0" if body_max_h is None else f"margin:0;overflow-y:scroll;max-height:{body_max_h}px"
    tc = "".join(f"<div>tool line {i} padded content for scroll range here</div>" for i in range(tc_lines))
    html = ("<html><body style='" + body_css + "'>"
            + "<div id='tool-section'><div id='tool-content' "
            + "style='max-height:200px;overflow-y:scroll'>" + tc + "</div></div>"
            + "<div id='content-placeholder'><p>short body text</p></div>"
            + "</body></html>")
    view.setHtml(html)
    view.setMinimumHeight(400)
    card_lay.addWidget(view)
    lay.addWidget(card)
    area.setWidgetResizable(True)
    area.setWidget(host_w)
    area.show()
    pump(2500)
    if force_geom is not None:
        sh, st, ch = force_geom
        view._on_body_geometry_reported(sh, st, ch)
    if force_inner is not None:
        view._on_inner_scrollable_reported(force_inner)
    _KEEPALIVE.append((area, host_w, card, view))
    return view, card


def read_tool_top(view):
    box = []
    view.page().runJavaScript(READ_JS, lambda r: box.append(r))
    pump(150)
    return box[0] if box else "?"


def click_delegate(view):
    fp = view.focusProxy()
    if fp is None:
        return
    ev = QMouseEvent(QEvent.MouseButtonPress, QPoint(50, 50), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(fp, ev)
    pump(50)


def _wheel(dy):
    return QWheelEvent(
        QPointF(20, 20), QPointF(20, 20), QPoint(0, 0), QPoint(0, dy),
        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False,
    )


def run_case(tag, body_max_h, tc_lines, force_geom, force_inner, armed, ticks=4):
    print(f"=== {tag} ===")
    view, card = build(body_max_h, tc_lines, force_geom, force_inner)
    if armed:
        click_delegate(view)
    # 桩滚动区必须在 build 前就挂到 _parent 链上（route 沿父链实时查找）
    outer = FakeArea()
    card._parent = type("H", (), {"chat_scroll_area": outer})()
    fp = view.focusProxy()
    print(f"armed={view._wheel_inner_armed} body_max_scroll={view._inner_scroll_range()[1]} "
          f"inner_scrollable={view._inner_scrollable} tool_top0={read_tool_top(view)} "
          f"filter_target_is_fp={view._wheel_filter_target is fp}")
    t0 = read_tool_top(view)
    tool_moved = 0
    for idx in range(ticks):
        # 走 notify 路径：直接 sendEvent 不保证触发挂在 fp 上的 eventFilter
        ev = _wheel(-120)
        belongs = view._wheel_belongs_to_outer(-120)
        consumed = view.eventFilter(fp, ev)
        print(f"    [tick{idx}] belongs_outer={belongs} eventFilter->{consumed} "
              f"outer._got={outer._got} parent_chain_ok={view.parent() is card}")
        if not consumed:
            QApplication.sendEvent(fp, ev)
        pump(300)
        t1 = read_tool_top(view)
        if t1 != t0:
            tool_moved += 1
        t0 = t1
    print(f"结果: tool-content动 {tool_moved}/{ticks}  外层被转发 {outer._got} 次  "
          f"card.wheelEvent={card._wheel_hits}  tool_top终={t0}")
    return tool_moved, outer._got


def main():
    l = run_case("L armed + tool-content可滚 + body不溢出", None, 60, (480, 0, 480), True, True)
    m = run_case("M 未点击 + 同几何（外层优先基线）", None, 60, (480, 0, 480), True, False)
    n = run_case("N armed + 内外都不可滚（B2 回归）", None, 2, (480, 0, 480), False, True)
    o = run_case("O armed + body 溢出（C 回归）", 300, 60, (1200, 0, 400), True, True)
    print(f"SUMMARY L(tool,outer)={l} M={m} N={n} O={o}")
    print("DONE-INNER")
    sys.exit(0)


if __name__ == "__main__":
    main()
