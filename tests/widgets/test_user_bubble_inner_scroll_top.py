# -*- coding: utf-8 -*-
"""回归：超长用户消息发送后，气泡**内部**滚动条贴底（显示尾部）而非从头显示。

根因
----
PlainTextViewer.append_chunk 用 ``QTextCursor(document)`` + ``movePosition(End)``
插入文本，共享 QTextDocument 让 QTextEdit 的**可见光标**落到文档末尾；随后气泡
定型 ``setFixedSize`` 触发 QTextEdit 重排 ``ensureCursorVisible`` → 内部垂直
滚动条被拉到末尾。实测（离屏，60 行长文本）：挂布局后 ``value=1032/max=1036``
（贴底）；只 ``setValue(0)`` 无效——后续重排按末尾光标再次 ensure 到底。

修复
----
append_chunk 插入后把可见光标归位到文档开头（PlainTextViewer 唯一使用者是
user 卡，不承担流式跟随底部职责）。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget

from app.widgets.message_card import MessageCard

_LONG = "\n".join(f"第{i}行用户输入内容 xxxxxxxxxxxxxxxxxxxxxxxx" for i in range(60))


def _flush(ms=200):
    qapp = QApplication.instance() or QApplication(sys.argv)
    loop = QTimer()
    loop.setSingleShot(True)
    loop.start(ms)
    while loop.isActive():
        qapp.processEvents()


def _build_card_in_area():
    """建一张长文本 user 卡挂进 QScrollArea，并把外层滚到底（复刻发送链路）。"""
    area = QScrollArea()
    area.setWidgetResizable(True)
    inner = QWidget()
    lay = QVBoxLayout(inner)
    lay.setContentsMargins(0, 0, 0, 0)
    area.setWidget(inner)
    area.resize(800, 500)
    area.show()

    card = MessageCard(parent=None, role="user")
    card.update_content(_LONG)
    card.finish_streaming()
    lay.addWidget(card, 0, Qt.AlignRight)
    _flush()

    outer = area.verticalScrollBar()
    outer.setValue(outer.maximum())
    _flush()
    return area, card


def test_user_bubble_inner_scrollbar_starts_at_top():
    """气泡内部滚动条初始必须在顶部（max>0 证明内容确实溢出、内滚条真实存在）。"""
    area, card = _build_card_in_area()
    try:
        v = card.viewer
        assert v is not None, "user 卡长文本应有 PlainTextViewer"
        sb = v.text_edit.verticalScrollBar()
        assert sb.maximum() > 0, f"长文本应出现内部滚动条，实际 max={sb.maximum()}"
        assert sb.value() == 0, f"气泡内部滚动条应从顶部开始，实际 value={sb.value()}"
    finally:
        area.deleteLater()


def test_user_bubble_cursor_home_after_append():
    """append_chunk 插入后可见光标归位文档开头（贴底根因的直接断言）。"""
    area, card = _build_card_in_area()
    try:
        v = card.viewer
        assert v.text_edit.textCursor().position() == 0
        v.append_chunk("\n追加一行 xxxx")
        _flush(120)
        assert v.text_edit.verticalScrollBar().value() == 0, "再次 append 后不得被拉到底"
    finally:
        area.deleteLater()
