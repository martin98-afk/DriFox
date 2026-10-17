# -*- coding: utf-8 -*-
"""探针 AJ：拦截 QScrollArea 的滚动入口，确认焦点引起的 value 跳变走的哪条路径（用完即删）。"""

import sys
import traceback

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton, QScrollArea, QVBoxLayout, QWidget

app = QApplication.instance() or QApplication([])


class SpyArea(QScrollArea):
    def ensureVisible(self, x, y, xmargin=50, ymargin=50):
        print(f"!!! ensureVisible(x={x}, y={y}, xm={xmargin}, ym={ymargin})")
        for fr in traceback.extract_stack()[:-1][-6:]:
            print(f"      {fr.filename.split(chr(92))[-1]}:{fr.lineno} {fr.name}")
        sys.stdout.flush()
        return super().ensureVisible(x, y, xmargin, ymargin)

    def scrollContentsBy(self, dx, dy):
        print(f"!!! scrollContentsBy(dx={dx}, dy={dy})  value_now={self.verticalScrollBar().value()}")
        for fr in traceback.extract_stack()[:-1][-6:]:
            print(f"      {fr.filename.split(chr(92))[-1]}:{fr.lineno} {fr.name}")
        sys.stdout.flush()
        return super().scrollContentsBy(dx, dy)

    def focusNextPrevChild(self, nxt):
        print(f"!!! focusNextPrevChild({nxt})")
        sys.stdout.flush()
        return super().focusNextPrevChild(nxt)


area = SpyArea()
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
    b = QPushButton(f"b{i}", w)
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

sb = area.verticalScrollBar()
sb.setValue(sb.maximum())
for _ in range(2):
    app.processEvents()
print(f"at bottom: value={sb.value()} max={sb.maximum()}")

btns[0].setFocus(Qt.OtherFocusReason)
for _ in range(2):
    app.processEvents()
print(f"focus set: value={sb.value()}")

print("\n=== 删除 card0 ===")
w0 = cards[0]
lay.removeWidget(w0)
w0.hide()
for _ in range(2):
    app.processEvents()
print(f"after hide: value={sb.value()}")
w0.setParent(None)
w0.deleteLater()
for _ in range(3):
    app.processEvents()
print(f"after delete: value={sb.value()}")
