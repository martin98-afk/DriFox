# -*- coding: utf-8 -*-
"""探针 G：对照实验——定位「回收时 value 跳变」的诱发条件（用完即删）。

变量：widgetResizable(False 默认 / True)、AlignBottom、占位机制。
卡用普通 QWidget（固定 300 高），排除 MessageCard 干扰。
"""

import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget

app = QApplication.instance() or QApplication([])

VIEWPORT_H = 400
N = 30
CARD_H = 300


def build(*, resizable: bool, align_bottom: bool, use_placeholder: bool):
    area = QScrollArea()
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
    sb.setValue(sb.maximum())
    for _ in range(2):
        app.processEvents()
    print(f"    at_bottom: value={sb.value()} max={sb.maximum()} c_h={container.height()} hint={container.sizeHint().height()}")

    # 回收上方 20 张：先装占位（可选）再逐张删卡
    for i in range(20):
        if use_placeholder:
            ph = QWidget(container)
            ph.setFixedHeight(CARD_H)
            layout_idx = lay.indexOf(cards[i])
            lay.insertWidget(layout_idx, ph)
        w = cards[i]
        lay.removeWidget(w)
        w.setParent(None)
        w.deleteLater()
        if i in (0, 1, 2, 5, 19):
            print(f"    del#{i}: value={sb.value()} max={sb.maximum()} c_h={container.height()} hint={container.sizeHint().height()}")

    for _ in range(3):
        app.processEvents()
    print(f"    final:     value={sb.value()} max={sb.maximum()} c_h={container.height()} hint={container.sizeHint().height()}")
    return area


for resizable in (False, True):
    for align_bottom in (False, True):
        for use_ph in (False, True):
            print(f"[resizable={resizable} align_bottom={align_bottom} placeholder={use_ph}]")
            build(resizable=resizable, align_bottom=align_bottom, use_placeholder=use_ph)
            print()
