# -*- coding: utf-8 -*-
"""hover 几何探针：鼠标划过卡片是否改变任何几何（列表微跳的来源验证）。

模拟 enterEvent/leaveEvent（MessageCard 内部只对 user 卡切按钮显隐），
对比前后：卡片高度、heightChanged 发射次数、滚动条 maximum/value。

用法：uv run python tests/debug/probe_hover_geometry.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")

# WebEngine 必须在 QApplication 创建前导入（共享 OpenGL 上下文要求）
from PyQt5 import QtWebEngineWidgets  # noqa: E402,F401
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget

app = QApplication(sys.argv)

from app.widgets.message_card import MessageCard  # noqa: E402

scroll = QScrollArea()
scroll.setWidgetResizable(True)
container = QWidget()
lay = QVBoxLayout(container)
lay.setContentsMargins(6, 6, 6, 6)
lay.setSpacing(6)

user_card = MessageCard(role="user", parent=container)
lay.addWidget(user_card)

assistant_card = MessageCard(role="assistant", parent=container)
lay.addWidget(assistant_card)

scroll.setWidget(container)
scroll.resize(600, 500)
scroll.show()
app.processEvents()


def snapshot(tag: str) -> None:
    sb = scroll.verticalScrollBar()
    parts = []
    for card in (user_card, assistant_card):
        parts.append(f"{card.role}:h={card.height()},hc={getattr(card, '_probe_hc_count', 0)}")
    print(f"[{tag}] sb.value={sb.value()} sb.max={sb.maximum()} | {' | '.join(parts)}")


for card in (user_card, assistant_card):
    card._probe_hc_count = 0
    card.heightChanged.connect(lambda *_c, _card=card: setattr(_card, "_probe_hc_count", _card._probe_hc_count + 1))

snapshot("基线")

app.sendEvent(user_card, QEvent(QEvent.Enter))
app.processEvents()
snapshot("user Enter")
app.sendEvent(user_card, QEvent(QEvent.Leave))
app.processEvents()
snapshot("user Leave")

app.sendEvent(assistant_card, QEvent(QEvent.Enter))
app.processEvents()
snapshot("assistant Enter")
app.sendEvent(assistant_card, QEvent(QEvent.Leave))
app.processEvents()
snapshot("assistant Leave")

for card in (user_card, assistant_card):
    me = QMouseEvent(QMouseEvent.MouseMove, QPoint(10, 10), QPoint(10, 10), Qt.NoButton, Qt.NoButton, Qt.NoModifier)
    app.sendEvent(card, me)
app.processEvents()
snapshot("MouseMove both")

print("done")
