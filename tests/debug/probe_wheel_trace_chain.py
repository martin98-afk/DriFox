# -*- coding: utf-8 -*-
"""探针 v2：定位 armed 黑洞链路（合成事件版，规避 SendInput 焦点/capture artifact）。

  T1 reportHeight 直调返回值 + console 消息计数（JS→console→Qt 信号断点）
  T2 合成 wheel 直发 fp：armed + geom 无效 → 外层是否动（复现真黑洞）
  T3 灌几何后 armed + 合成 wheel → 外层是否动（belongs_to_outer 兜底接管？）
  T4 armed + body 溢出 + 合成 wheel → 链式（卡内滚到边界交外层）
"""

import ctypes
import os
import sys
import time
from unittest.mock import MagicMock

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PyQt5.QtCore import QEvent, QObject, QPointF, QPoint, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent, QWheelEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication([])

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402

try:
    init_shared_web_profile()
except Exception:  # noqa: BLE001
    pass

from app.main_widget import OpenAIChatToolWindow  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_wheel_real_host import build_window, click_delegate, pump  # noqa: E402


def state(view):
    return (f"geom_valid={view._body_geom_valid} "
            f"max_scroll={view._inner_scroll_range()[1]} "
            f"inner_scrollable={view._inner_scrollable} "
            f"armed={view._wheel_inner_armed}")


def wheel_at_fp(view, dy):
    """合成 wheel 直发 fp（真实第一接收者），走完整 filter→delegate→Chromium 链。"""
    fp = view.focusProxy()
    c = fp.rect().center()
    ev = QWheelEvent(QPointF(c), QPointF(fp.mapToGlobal(c)), QPoint(0, 0), QPoint(0, dy),
                     Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    return QApplication.sendEvent(fp, ev)


def main():
    print("=== T1 console 链 + reportHeight 返回值 ===")
    win, area, container, cards = build_window()
    card = cards[2]
    view = card.viewer
    console_hits = {"n": 0}

    orig_jscm = view._page.javaScriptConsoleMessage

    def spy_jscm(level, message, line, src):
        if message.startswith("pywebview_height:"):
            console_hits["n"] += 1
        orig_jscm(level, message, line, src)

    view._page.javaScriptConsoleMessage = spy_jscm
    print(f"渲染后: {state(view)} console_pywebview_height={console_hits['n']}")
    box = []
    view.page().runJavaScript(
        "(function(){ try { reportHeight(); return 'ok:' + document.body.scrollHeight + '|' "
        "+ document.body.clientHeight + '|' + document.body.scrollTop; } catch(e) { return 'ERR:' + e.message; } })()",
        lambda r: box.append(r),
    )
    pump(800)
    print(f"reportHeight 直调: 返回={box} console_pywebview_height={console_hits['n']} {state(view)}")

    print("=== T2 armed + geom无效 + 合成wheel → 外层？ ===")
    click_delegate(view)
    print(f"点击后: {state(view)}")
    vbar = area.verticalScrollBar()
    moved = 0
    for i in range(5):
        v0 = vbar.value()
        wheel_at_fp(view, -120)
        pump(400)
        m = vbar.value() != v0
        moved += m
        print(f"  tick{i}: vbar {v0}->{vbar.value()} 外层动={m} {state(view)}")
    print(f"结果: 外层滚动 {moved}/5")

    print("=== T3 灌几何 + armed + 合成wheel → belongs_to_outer 兜底 ===")
    view._on_body_geometry_reported(400, 0, 400)
    view._on_inner_scrollable_reported(False)
    moved = 0
    for i in range(4):
        v0 = vbar.value()
        wheel_at_fp(view, -120)
        pump(400)
        m = vbar.value() != v0
        moved += m
        print(f"  tick{i}: vbar {v0}->{vbar.value()} 外层动={m} {state(view)}")
    print(f"结果: 外层滚动 {moved}/4")

    print("=== T4 armed + body溢出 + 合成wheel → 链式 ===")
    # 造长内容：直接往 body 塞高
    box.clear()
    view.page().runJavaScript(
        "(function(){ var d=document.createElement('div');"
        "d.style.height='2000px'; d.id='probe-filler';"
        "document.body.appendChild(d); reportHeight(); return 'ok'; })()",
        lambda r: box.append(r),
    )
    pump(600)
    print(f"塞高后: {state(view)}")
    moved = 0
    for i in range(8):
        v0 = vbar.value()
        wheel_at_fp(view, -120)
        pump(400)
        m = vbar.value() != v0
        moved += m
        print(f"  tick{i}: vbar {v0}->{vbar.value()} 外层动={m} {state(view)}")
    print(f"结果: 外层滚动 {moved}/8")
    print("DONE-CHAIN2")
    sys.exit(0)


if __name__ == "__main__":
    main()
