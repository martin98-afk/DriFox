# -*- coding: utf-8 -*-
"""探针 T：对比焦点保护方式：clearFocus vs setFocus(area)（用完即删）。"""

import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QPushButton, QScrollArea, QVBoxLayout, QWidget

app = QApplication.instance() or QApplication([])


def build(with_btn=True):
    area = QScrollArea()
    container = QWidget()
    lay = QVBoxLayout(container)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    lay.setAlignment(Qt.AlignBottom)
    area.setWidget(container)
    area.setWidgetResizable(True)
    area.resize(500, 400)

    cards, btns = [], []
    for i in range(30):
        w = QWidget(container)
        w.setFixedHeight(300)
        inner = QVBoxLayout(w)
        if with_btn:
            b = QPushButton(f"b{i}", w)
            inner.addWidget(b)
            btns.append(b)
        lay.addWidget(w)
        cards.append(w)

    area.show()
    for _ in range(3):
        app.processEvents()
    container.adjustSize()
    for _ in range(3):
        app.processEvents()
    return area, container, lay, cards, btns


def run(tag, protect):
    area, container, lay, cards, btns = build()
    sb = area.verticalScrollBar()
    sb.setValue(sb.maximum())
    for _ in range(2):
        app.processEvents()
    btns[0].setFocus(Qt.OtherFocusReason)
    for _ in range(2):
        app.processEvents()
    v0 = sb.value()

    protect(area, btns)

    w0 = cards[0]
    # 模拟 delete_widgets_from_layout 的次序
    lay.removeWidget(w0)
    w0.hide()
    w0.setParent(None)
    w0.deleteLater()
    for _ in range(3):
        app.processEvents()

    v1 = sb.value()
    print(f"[{tag}] v0={v0} -> v1={v1}  drift={v1 - v0:+d}  {'OK' if abs(v1 - v0) <= 2 else 'JUMP'}")
    print(f"    focus now: {app.focusWidget()}")


run("S  无保护", lambda area, btns: None)
run("C1 fw.clearFocus()", lambda area, btns: app.focusWidget().clearFocus())
run("C2 area.setFocus()", lambda area, btns: area.setFocus(Qt.OtherFocusReason))
run("C3 fw 变 NoFocus", lambda area, btns: btns[0].setFocusPolicy(Qt.NoFocus))
