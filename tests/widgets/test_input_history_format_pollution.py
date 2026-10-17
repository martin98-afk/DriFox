# -*- coding: utf-8 -*-
"""输入历史切换后发送只剩附件（正文被折叠进胶囊格式）的回归测试

根因（2026-10-13 排障）：
1. 历史切换链路把光标置尾，光标停在胶囊（U+FFFC）之后时 charFormat 残留
   胶囊 objectType/属性；
2. 下一次 setPlainText 内部用 control cursor insertText，整段新文本继承该
   格式 → 普通文字的 fragment 带胶囊格式；
3. 此后文档中只要存在一枚真胶囊，重写版 toPlainText 走 _expand_mention_objects
   展开路径，被污染的文字 fragment 被折叠成 [[旧文件名]] → 发送链路
   _build_user_text_with_attachments 清除不匹配占位符后只剩附件路径。

修复两层：
- setPlainText 覆盖：写入前清空光标 charFormat（根治污染源）
- _expand_mention_objects 加固：只把「单个 U+FFFC + object 格式」当胶囊
  （防御：即使格式被污染也不丢正文）
"""

import os
import sys

from PySide6.QtGui import QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QApplication

from app.widgets.bottom_input_area import (
    _FILE_MENTION_PATH_PROP,
    _FILE_MENTION_TYPE,
    SendableTextEdit,
)


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


def _append_mention_at_end(area: SendableTextEdit, path: str):
    """在文末插入一枚文件胶囊并把光标停在胶囊之后（历史切换后的真实状态）"""
    cursor = area.textCursor()
    cursor.movePosition(QTextCursor.End)
    cursor.insertText("\ufffc", SendableTextEdit._make_mention_format(path))
    area.setTextCursor(cursor)
    cursor.movePosition(QTextCursor.End)
    area.setTextCursor(cursor)


def _dump_object_types(area: SendableTextEdit) -> list[int]:
    types = []
    block = area.document().begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid():
                types.append(frag.charFormat().objectType())
            it += 1
        block = block.next()
    return types


def test_setPlainText_does_not_inherit_mention_format(qapp):
    """光标停在胶囊后，setPlainText 的新文本不得继承胶囊格式（根因直测）"""
    _ensure_qapp()
    area = SendableTextEdit()
    area.setPlainText("引用已删除附件 ")
    _append_mention_at_end(area, r"D:\fake\ghost.txt")
    assert area.textCursor().charFormat().objectType() == _FILE_MENTION_TYPE

    # 光标 charFormat 已被污染（Qt 原生行为），setPlainText 必须自行清洗
    area.setPlainText("[[a.txt]] 和 [[a.txt]] 再和 [[a.txt]]")
    types = _dump_object_types(area)
    assert all(t == 0 for t in types), f"新文本 fragment 仍带 object 格式: {types}"
    assert area.toPlainText() == "[[a.txt]] 和 [[a.txt]] 再和 [[a.txt]]"


def test_history_switch_after_mention_entry_keeps_text(qapp):
    """症状级复现：浏览带附件的历史条目后再切其它条目，正文不得丢失

    修复前：切到带胶囊的条目后光标停在胶囊尾，下一次 setPlainText 的整段
    新文本被污染成胶囊格式，toPlainText 展开时折叠为 [[旧文件名]]。
    """
    _ensure_qapp()
    area = SendableTextEdit()
    history = [
        {"text": "帮我分析这个文件 [[a.txt]] 谢谢", "attachments": []},  # 附件不存在 → 胶囊仍插入
        {"text": "纯文字消息没有附件", "attachments": []},
    ]
    area.load_history(history)
    area._navigate_history(1)  # 进入条目0（含胶囊）
    assert "[[a.txt]]" in area.toPlainText()
    area._navigate_history(1)  # 切到条目1（纯文字）
    assert area.toPlainText() == "纯文字消息没有附件"


def test_expand_mention_objects_ignores_polluted_fragments(qapp):
    """防御层：多字符文本即使被污染成胶囊格式，展开时也按普通文本输出"""
    _ensure_qapp()
    area = SendableTextEdit()
    # 构造污染态：真胶囊 + 一段带胶囊格式的普通文本（绕过 setPlainText 覆盖直接注入）
    area.setPlainText("A ")
    _append_mention_at_end(area, r"D:\fake\b.txt")
    doc = area.document()
    cursor = QTextCursor(doc)
    cursor.movePosition(QTextCursor.End)
    polluted = QTextCharFormat()
    polluted.setObjectType(_FILE_MENTION_TYPE)
    polluted.setProperty(_FILE_MENTION_PATH_PROP, r"D:\fake\ghost.txt")
    cursor.insertText("这段普通文字被污染", polluted)
    area.setTextCursor(cursor)

    expanded = area.toPlainText()
    assert "[[b.txt]]" in expanded  # 真胶囊正常展开
    assert "这段普通文字被污染" in expanded  # 污染文本按原样输出，不折叠


def test_real_mention_expansion_unaffected(qapp):
    """防误伤：正常胶囊展开行为保持不变"""
    _ensure_qapp()
    area = SendableTextEdit()
    area.setPlainText("前文 ")
    _append_mention_at_end(area, r"D:\dir\note.md")
    assert area.toPlainText() == "前文 [[note.md]]"
    assert area.textCursor().charFormat().stringProperty(_FILE_MENTION_PATH_PROP) == r"D:\dir\note.md"
