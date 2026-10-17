# -*- coding: utf-8 -*-
"""探针 M：最小 Qt——QScrollArea 内焦点转移是否引起 value 跳变（用完即删）。"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton, QScrollArea, QVBoxLayout, QWidget

app = QApplication.instance() or QApplication([])

area = QScrollArea()
container = QWidget()
lay = QVBoxLayout(container)
lay.setContentsMargins(0, 0, 0, 0)
lay.setSpacing(0)
lay.setAlignment(Qt.AlignBottom)
area.setWidget(container)
area.setWidgetResizable(True)
area.resize(500, 400)

btns = []
for i in range(30):
    w = QWidget()
    w.setFixedHeight(300)
    inner = QVBoxLayout(w)
    b = QPushButton(f"b{i}")
    inner.addWidget(b)
    lay.addWidget(w)
    btns.append((w, b))

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

# case 1: 无焦点相关，直接移除顶部 widget
w0, b0 = btns[0]
lay.removeWidget(w0)
w0.setParent(None)
w0.deleteLater()
for _ in range(2):
    app.processEvents()
print(f"case1 remove w0 (no focus): value={sb.value()} max={sb.maximum()}")

# case 2: 让按钮获得焦点，再移除它所在的 widget
sb.setValue(sb.maximum())
for _ in range(2):
    app.processEvents()
print(f"\nreset: value={sb.value()}")
w5, b5 = btns[5]
b5.setFocus()
for _ in range(2):
    app.processEvents()
print(f"after b5.setFocus: value={sb.value()} max={sb.maximum()} focus={app.focusWidget()}")

print(f"focusWidget local name: {app.focusWidget().text() if hasattr(app.focusWidget(), 'text') else app.focusWidget()}")
lay.removeWidget(w5)
w5.hide()
for _ in range(2):
    app.processEvents()
print(f"after hide w5 (contains focused b5): value={sb.value()} max={sb.maximum()} focus={app.focusWidget()}")
