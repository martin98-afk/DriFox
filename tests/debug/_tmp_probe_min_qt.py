# -*- coding: utf-8 -*-
"""探针 F：最小 Qt 实验——QScrollArea 内容顶部删除 widget 时 value/max 演化。

对照探针 C/D：怀疑「逐张删除上方 widget」本身会引起 Qt 内部 value 重算。
"""

import sys

from PyQt5.QtCore import Qt

from PyQt5.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget

app = QApplication.instance() or QApplication([])

area = QScrollArea()
container = QWidget()
lay = QVBoxLayout(container)
lay.setContentsMargins(0, 0, 0, 0)
lay.setSpacing(0)
lay.setAlignment(Qt.AlignBottom)  # 对照 chat_layout
area.setWidget(container)
area.setWidgetResizable(True)
area.resize(500, 400)

cards = []
for i in range(30):
    w = QWidget()
    w.setFixedHeight(300)
    lay.addWidget(w)
    cards.append(w)

area.show()
for _ in range(3):
    app.processEvents()
container.adjustSize()
for _ in range(3):
    app.processEvents()

sb = area.verticalScrollBar()
print(f"init: value={sb.value()} max={sb.maximum()} hint={container.sizeHint().height()} h={container.height()}")

sb.setValue(sb.maximum())
app.processEvents()
print(f"at bottom: value={sb.value()} max={sb.maximum()} hint={container.sizeHint().height()} h={container.height()}")

print("\n=== 从顶部逐张删除 20 张（不跑事件循环）===")
for i in range(20):
    w = cards[i]
    lay.removeWidget(w)
    w.setParent(None)
    w.deleteLater()
    print(f"  del#{i}: value={sb.value()} max={sb.maximum()} hint={container.sizeHint().height()} h={container.height()}")

print("\n=== 跑事件循环 ===")
for _ in range(5):
    app.processEvents()
print(f"after events: value={sb.value()} max={sb.maximum()} hint={container.sizeHint().height()} h={container.height()}")
