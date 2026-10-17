# -*- coding: utf-8 -*-
"""探针 R：验证「删除前把焦点移出被删子树」能否避免自动滚动（用完即删）。

对照组：
S: 焦点在将被删除卡的按钮上 → 直接删 → value 跳（复现）
R: 同上，但删前 setFocus(area) → 期望 value 保持
T: 同上，但删前 setFocus(外部按钮) → 期望 value 保持
U: 焦点在「不被删除」的卡上 → 删别的卡 → 期望 value 保持（不能误伤）
"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton, QScrollArea, QVBoxLayout, QWidget

app = QApplication.instance() or QApplication([])


def build():
    area = QScrollArea()
    container = QWidget()
    lay = QVBoxLayout(container)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    lay.setAlignment(Qt.AlignBottom)
    area.setWidget(container)
    area.setWidgetResizable(True)
    area.resize(500, 400)

    outside = QPushButton("outside")
    outside.show()

    cards, btns = [], []
    for i in range(30):
        w = QWidget()
        w.setFixedHeight(300)
        inner = QVBoxLayout(w)
        b = QPushButton(f"b{i}")
        inner.addWidget(b)
        lay.addWidget(w)
        cards.append(w)
        btns.append(b)

    area.show()
    for _ in range(3):
        app.processEvents()
    container.adjustSize()
    for _ in range(3):
        app.processEvents()
    return area, container, lay, cards, btns, outside


def run(tag, prep):
    area, container, lay, cards, btns, outside = build()
    sb = area.verticalScrollBar()
    sb.setValue(sb.maximum())
    for _ in range(2):
        app.processEvents()

    # 焦点放到卡 0 的按钮上（模拟：用户点过卡片）
    btns[0].setFocus(Qt.OtherFocusReason)
    for _ in range(2):
        app.processEvents()

    v0 = sb.value()
    prep(area, btns, outside)
    for _ in range(2):
        app.processEvents()
    v_prep = sb.value()

    # 删除卡 0（含焦点按钮）
    w0 = cards[0]
    lay.removeWidget(w0)
    w0.hide()
    for _ in range(2):
        app.processEvents()
    w0.setParent(None)
    w0.deleteLater()
    for _ in range(3):
        app.processEvents()

    v1 = sb.value()
    verdict = "OK 保持" if abs(v1 - v0) <= 2 else f"JUMP {v1 - v0:+d}"
    print(f"[{tag}] v0={v0} after_prep={v_prep} after_remove={v1}  -> {verdict}")


def p_none(area, btns, outside):
    pass


def p_area(area, btns, outside):
    area.setFocus(Qt.OtherFocusReason)


def p_outside(area, btns, outside):
    outside.setFocus(Qt.OtherFocusReason)


print("=== S: 无保护（对照组，应跳）===")
run("S", p_none)
print("\n=== R: 删前 setFocus(area) ===")
run("R", p_area)
print("\n=== T: 删前 setFocus(外部按钮) ===")
run("T", p_outside)
print("\n=== U: 焦点留在别处（不在被删卡），删卡0 ===")
run("U", lambda area, btns, outside: btns[5].setFocus(Qt.OtherFocusReason))
