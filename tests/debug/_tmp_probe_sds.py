# -*- coding: utf-8 -*-
"""探针 H：把 QScrollArea 换成 qfluentwidgets SingleDirectionScrollArea（用完即删）。"""

import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget

app = QApplication.instance() or QApplication([])

from qfluentwidgets import SingleDirectionScrollArea  # noqa: E402

VIEWPORT_H = 400
N = 30
CARD_H = 300


def build(*, resizable: bool, align_bottom: bool, placeholder: bool):
    area = SingleDirectionScrollArea()
    container = QWidget()
    lay = QVBoxLayout(container)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    if align_bottom:
        lay.setAlignment(Qt.AlignBottom)
    area.setWidget(container)
    if resizable:
        area.setWidgetResizable(True)
    area.resize(500, VIEWPORT_H)

    cards = []
    for i in range(N):
        w = QWidget()
        w.setFixedHeight(CARD_H)
        lay.addWidget(w)
        cards.append(w)

    area.show()
    for _ in range(3):
        app.processEvents()
    container.adjustSize()
    for _ in range(3):
        app.processEvents()

    sb = area.verticalScrollBar()
    print(f"    scrollbar type: {type(sb).__name__}")
    sb.setValue(sb.maximum())
    for _ in range(3):
        app.processEvents()
    print(f"    at_bottom: value={sb.value()} max={sb.maximum()} c_h={container.height()} hint={container.sizeHint().height()}")

    for i in range(20):
        if placeholder:
            ph = QWidget(container)
            ph.setFixedHeight(CARD_H)
            lay.insertWidget(lay.indexOf(cards[i]), ph)
        w = cards[i]
        lay.removeWidget(w)
        w.setParent(None)
        w.deleteLater()
        if i in (0, 1, 2, 5, 19):
            print(f"    del#{i}: value={sb.value()} max={sb.maximum()} c_h={container.height()} hint={container.sizeHint().height()}")

    for _ in range(6):
        app.processEvents()
    print(f"    final:     value={sb.value()} max={sb.maximum()} c_h={container.height()} hint={container.sizeHint().height()}")
    return area


for resizable in (False, True):
    for align_bottom in (False, True):
        for ph in (False, True):
            print(f"[SDS resizable={resizable} align_bottom={align_bottom} placeholder={ph}]")
            build(resizable=resizable, align_bottom=align_bottom, placeholder=ph)
            print()
