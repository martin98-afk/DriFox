# -*- coding: utf-8 -*-
"""探针 Y：受控变量——`hide()` 焦点转移引起 value 跳变的确切条件（用完即删）。

每次只改一个变量：
A 基线（复现）
B + setWidgetResizable(True)
C + AlignBottom
D + spacing/margins 6
E + 全部（对齐探针 X）
F 基线 + area.setFocus 保护（删前）
"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication([])

from app.widgets.message_card import MessageCard  # noqa: E402

VIEWPORT_H = 400
CARD_H = 300
N = 20


def p(*a):
    print(*a)
    sys.stdout.flush()


def build(resizable=False, align_bottom=False, spacing6=False):
    area = QScrollArea()
    container = QWidget()
    lay = QVBoxLayout(container)
    if spacing6:
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)
    else:
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
        card = MessageCard(role="assistant", parent=container)
        card.setFixedHeight(CARD_H)
        card._message_index = i
        lay.addWidget(card)
        cards.append(card)

    area.show()
    for _ in range(3):
        app.processEvents()
    container.adjustSize()
    for _ in range(3):
        app.processEvents()
    return area, container, lay, cards


def run(tag, protect=None, **cfg):
    area, container, lay, cards = build(**cfg)
    sb = area.verticalScrollBar()

    # 焦点放到卡0内按钮
    card0 = cards[0]
    focusables = [c for c in card0.findChildren(QWidget) if c.focusPolicy() != Qt.NoFocus]
    fw = focusables[0] if focusables else None
    if fw is not None:
        fw.setFocus(Qt.OtherFocusReason)
        for _ in range(2):
            app.processEvents()
        p(f'    [focus check] wanted={type(fw).__name__} visible={fw.isVisible()} actual={type(QApplication.focusWidget()).__name__ if QApplication.focusWidget() else None} in_card0={card0.isAncestorOf(QApplication.focusWidget()) if QApplication.focusWidget() else None}')

    sb.setValue(sb.maximum())
    for _ in range(3):
        app.processEvents()
    v0 = sb.value()

    # 让焦点位于待删卡上
    if fw is not None:
        fw.setFocus(Qt.OtherFocusReason)
        for _ in range(2):
            app.processEvents()

    if protect:
        protect(area)

    # 删除卡 0（对齐 delete_widgets_from_layout：removeWidget → hide → setParent → deleteLater）
    lay.removeWidget(card0)
    card0.hide()
    for _ in range(2):
        app.processEvents()
    card0.setParent(None)
    card0.deleteLater()
    for _ in range(4):
        app.processEvents()

    v1 = sb.value()
    verdict = "OK" if abs(v1 - v0) <= 30 else f"JUMP {v1 - v0:+d}"
    p(f"[{tag}] v0={v0} -> v1={v1}   {verdict}")
    return v1 - v0


p("=== 变量矩阵（焦点在卡0按钮上，删卡0）===")
run("A 基线(全默认)", )
run("B +resizable", resizable=True)
run("C +AlignBottom", align_bottom=True)
run("D +spacing6", spacing6=True)
run("E 全开", resizable=True, align_bottom=True, spacing6=True)
run("F 基线+删前area.setFocus", protect=lambda area: area.setFocus(Qt.OtherFocusReason))
run("G 基线+删前clearFocus", protect=lambda area: (QApplication.focusWidget() or area).clearFocus())
