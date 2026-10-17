# -*- coding: utf-8 -*-
"""探针 N：QScrollArea 删顶部 widget 时 value 跳变的触发条件矩阵（用完即删）。"""
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

app = QApplication.instance() or QApplication([])


def build(focusable: bool):
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
        w = QWidget()
        w.setFixedHeight(300)
        inner = QVBoxLayout(w)
        if focusable:
            b = QPushButton(f"b{i}")
            inner.addWidget(b)
            btns.append(b)
        else:
            inner.addWidget(QLabel(f"l{i}", w))
        lay.addWidget(w)
        cards.append(w)

    area.show()
    for _ in range(3):
        app.processEvents()
    container.adjustSize()
    for _ in range(3):
        app.processEvents()
    return area, container, lay, cards, btns


def case(tag, focusable, do_focus, hide_first):
    area, container, lay, cards, btns = build(focusable)
    sb = area.verticalScrollBar()
    sb.setValue(sb.maximum())
    for _ in range(2):
        app.processEvents()
    v0, m0 = sb.value(), sb.maximum()

    if do_focus and btns:
        btns[0].setFocus()
        for _ in range(2):
            app.processEvents()

    fw_before = app.focusWidget()
    w0 = cards[0]
    lay.removeWidget(w0)
    if hide_first:
        w0.hide()
        for _ in range(2):
            app.processEvents()
    w0.setParent(None)
    w0.deleteLater()
    for _ in range(3):
        app.processEvents()
    fw_after = app.focusWidget()
    print(f"[{tag}] focusable={focusable} focus={do_focus} hide_first={hide_first}")
    print(f"    v {v0}->{sb.value()}  max {m0}->{sb.maximum()}  fw_before={fw_before} fw_after={fw_after}")
    if btns:
        b1_topleft = btns[1].mapTo(container, btns[1].rect().topLeft())
        print(f"    b1 y_in_container={b1_topleft.y()}  b1 h={btns[1].height()}")
        print(f"    card1 y={cards[1].mapTo(container, cards[1].rect().topLeft()).y()}")


case("A 无焦点源", False, False, False)
case("B 有焦点源,不聚焦", True, False, False)
case("C 有焦点源,聚焦b0", True, True, False)
case("D 有焦点源,聚焦b0,hide先行", True, True, True)
