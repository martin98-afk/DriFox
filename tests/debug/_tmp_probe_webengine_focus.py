# -*- coding: utf-8 -*-
"""探针 W：离屏 QWebEngineView 焦点转移实验（用完即删）。

验证：
1. QWebEngineView 的 focusProxy 是谁、父子关系如何
2. 焦点在 viewer（或 delegate）上时 hide() 外层卡片是否引起 QScrollArea 滚动
"""

import os
import sys

os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu"
os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PySide6.QtCore import Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication([])

from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402


def p(*a):
    print(*a)
    sys.stdout.flush()


area = QScrollArea()
container = QWidget()
lay = QVBoxLayout(container)
lay.setContentsMargins(0, 0, 0, 0)
lay.setSpacing(0)
lay.setAlignment(Qt.AlignBottom)
area.setWidget(container)
area.setWidgetResizable(True)
area.resize(500, 400)

cards = []
viewers = []
for i in range(30):
    w = QWidget(container)
    w.setFixedHeight(300)
    inner = QVBoxLayout(w)
    if i == 0:
        v = QWebEngineView(w)
        v.setHtml("<h1>hi</h1>", baseUrl=QUrl("about:blank"))
        inner.addWidget(v)
        viewers.append(v)
    lay.addWidget(w)
    cards.append(w)

area.show()
for _ in range(5):
    app.processEvents()


def on_load(ok):
    p("=== load finished ===")
    v = viewers[0]
    p(f"viewer focusPolicy={v.focusPolicy()}")
    fp = v.focusProxy()
    p(f"viewer focusProxy={fp} ({type(fp).__name__ if fp else None})")
    if fp is not None:
        p(f"fp.childOf viewer? {v.isAncestorOf(fp)}   fp is v? {fp is v}")
        p(f"fp focusPolicy={fp.focusPolicy()}")

    sb = area.verticalScrollBar()
    sb.setValue(sb.maximum())
    for _ in range(3):
        app.processEvents()
    v0 = sb.value()
    p(f"at bottom: value={v0} max={sb.maximum()} container_h={container.height()}")

    # 焦点放到 viewer（模拟点击正文）
    v.setFocus(Qt.OtherFocusReason)
    for _ in range(3):
        app.processEvents()
    fw = app.focusWidget()
    p(f"after v.setFocus: focusWidget={fw} ({type(fw).__name__ if fw else None})")
    if fw is not None:
        p(f"    fw is v? {fw is v}; v.isAncestorOf(fw)? {v.isAncestorOf(fw)}")

    # 删除 card0（含 viewer）→ 观察
    from app.widgets.ui_helpers import delete_widgets_from_layout

    delete_widgets_from_layout([cards[0]], lay, call_cleanup=False)
    for _ in range(4):
        app.processEvents()
    v1 = sb.value()
    p(f"after delete card0: value={v1} drift={v1 - v0:+d} {'JUMP' if abs(v1 - v0) > 2 else 'OK-ish'}")
    p(f"focus now: {app.focusWidget()}")

    # 第二组：clearFocus 保护
    p("\n=== 第二组：clearFocus 保护 ===")
    sb.setValue(sb.maximum())
    for _ in range(3):
        app.processEvents()
    v2 = sb.value()
    v2w = viewers[0] if viewers else None
    if v2w is not None:
        v2w.setFocus(Qt.OtherFocusReason)
        for _ in range(2):
            app.processEvents()
    fw2 = app.focusWidget()
    if fw2 is not None and (fw2 is cards[1] or cards[1].isAncestorOf(fw2)):
        fw2.clearFocus()
    for _ in range(2):
        app.processEvents()
    delete_widgets_from_layout([cards[1]], lay, call_cleanup=False)
    for _ in range(4):
        app.processEvents()
    v3 = sb.value()
    p(f"after delete card1 (protect): value={v3} drift={v3 - v2:+d}")

    sys.stdout.flush()
    os._exit(0)


viewers[0].loadFinished.connect(on_load)

QTimer.singleShot(8000, lambda: (p("timeout"), sys.stdout.flush(), os._exit(2)))
app.exec_()
