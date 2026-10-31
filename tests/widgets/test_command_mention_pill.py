# -*- coding: utf-8 -*-
"""/ 命令胶囊（CommandMentionObject）机制回归测试。

覆盖：空格确认折叠、toPlainText 展开透明、Backspace 解封、
detail 参数定位的 plain↔doc 偏移换算、未知命令不折叠、
remove_parameter_text 后重折叠。
"""

import pytest

from app.widgets.bottom_input_area import (
    _COMMAND_MENTION_TYPE,
    SendableTextEdit,
)

KNOWN = {"cmd", "help"}


@pytest.fixture()
def editor(qapp, monkeypatch):
    """构造输入框，已知命令判定固定为 KNOWN 集合

    check 的 detail 分支与 _collapse_command_token 共用
    CommandManager.has_command / get_skill_by_name，patch 类级方法后
    两条判定路径同时被固定，无需单独 mock。
    """
    from app.core.commands.command_manager import CommandManager
    import app.utils.utils as utils_mod

    ed = SendableTextEdit()
    monkeypatch.setattr(CommandManager, "has_command", lambda self, name: name in KNOWN)
    monkeypatch.setattr(utils_mod, "get_skill_by_name", lambda name: object() if name in KNOWN else None)
    return ed


def _doc_text(editor) -> str:
    """原始文档文本（胶囊 = U+FFFC，不经展开）"""
    from qfluentwidgets import TextEdit

    return TextEdit.toPlainText(editor)


def _first_frag_is_command_pill(editor) -> bool:
    frag = editor.document().firstBlock().begin().fragment()
    if not frag.isValid():
        return False
    return (
        frag.length() <= 2
        and frag.text() == "\ufffc"
        and frag.charFormat().objectType() == _COMMAND_MENTION_TYPE
    )


def _type_at_end(editor, text: str):
    """模拟真实输入：光标先到末尾再插入（触发 textChanged 时光标在插入后）"""
    cursor = editor.textCursor()
    cursor.movePosition(cursor.End)
    editor.setTextCursor(cursor)
    editor.textCursor().insertText(text)


def test_space_confirmed_command_collapses(editor):
    """手打 "/cmd " 空格确认已知命令 → 自动折叠成胶囊，展开文本不变"""
    editor.setPlainText("/cmd")
    _type_at_end(editor, " ")

    assert _first_frag_is_command_pill(editor)
    assert "\ufffc" in _doc_text(editor)
    assert editor.toPlainText() == "/cmd "


def test_expand_keeps_params_transparent(editor):
    """折叠后继续输入参数，toPlainText 与纯文本时代逐字符一致"""
    editor.setPlainText("/cmd")
    _type_at_end(editor, " ")
    _type_at_end(editor, "--verbose x")

    assert editor.toPlainText() == "/cmd --verbose x"


def test_unknown_command_stays_plain(editor):
    """未知命令 + 空格 → 保持纯文本，不折叠"""
    editor.setPlainText("/nope")
    _type_at_end(editor, " ")

    assert not _first_frag_is_command_pill(editor)
    assert "\ufffc" not in _doc_text(editor)
    assert editor.toPlainText() == "/nope "


def test_backspace_unfolds_without_deleting(editor):
    """Backspace 第一次只解封：胶囊变回纯文本，命令名完好，光标在文本尾"""
    editor.setPlainText("/cmd")
    _type_at_end(editor, " ")
    assert _first_frag_is_command_pill(editor)

    cursor = editor.textCursor()
    cursor.setPosition(1)  # 胶囊后
    editor.setTextCursor(cursor)

    assert editor._unfold_command_if_at_cursor(backspace=True) is True
    assert "\ufffc" not in _doc_text(editor)
    assert editor.toPlainText() == "/cmd "
    # 光标落在命令名末尾（空格之前）：胶囊右边缘对应 "/cmd" 结尾，
    # 不是空格之后
    assert editor.textCursor().position() == len("/cmd")


def test_delete_unfolds_keeps_cursor(editor):
    """光标在胶囊前按 Delete：解封且光标原地不动"""
    editor.setPlainText("/cmd")
    _type_at_end(editor, " ")
    cursor = editor.textCursor()
    cursor.setPosition(0)
    editor.setTextCursor(cursor)

    assert editor._unfold_command_if_at_cursor(backspace=False) is True
    assert "\ufffc" not in _doc_text(editor)
    assert editor.textCursor().position() == 0


def test_backspace_away_from_pill_returns_false(editor):
    """光标不贴胶囊时解封拦截不生效（交还默认删除行为）"""
    editor.setPlainText("/cmd")
    _type_at_end(editor, " ")
    cursor = editor.textCursor()
    cursor.movePosition(cursor.End)
    editor.setTextCursor(cursor)

    assert editor._unfold_command_if_at_cursor(backspace=False) is False


def test_partial_param_replacement_with_pill(editor):
    """胶囊存在时点击参数补全：--subag 原地替换为 --subagent（偏移换算）"""
    editor.setPlainText("/cmd")
    _type_at_end(editor, " ")
    _type_at_end(editor, "--subag")
    assert editor.toPlainText() == "/cmd --subag"

    editor.insert_parameter_text("--subagent", "flag")

    assert editor.toPlainText() == "/cmd --subagent "


def test_insert_parameter_after_pill(editor):
    """胶囊存在时光标在末尾补全 flag 参数：无重复空格、位置正确"""
    editor.setPlainText("/cmd")
    _type_at_end(editor, " ")
    cursor = editor.textCursor()
    cursor.movePosition(cursor.End)
    editor.setTextCursor(cursor)

    editor.insert_parameter_text("--verbose", "flag")

    assert editor.toPlainText() == "/cmd --verbose "


def test_remove_parameter_refolds(editor):
    """删除参数走 setPlainText 全文重写后，命令胶囊应重新折叠"""
    editor.setPlainText("/cmd")
    _type_at_end(editor, " ")
    _type_at_end(editor, "--verbose ")
    assert editor.toPlainText() == "/cmd --verbose "

    editor.remove_parameter_text("--verbose", "flag")

    assert editor.toPlainText() == "/cmd "
    assert _first_frag_is_command_pill(editor)


def test_value_selection_replaces_partial_input(editor):
    """值选择补全：--model= 后的部分输入被选区替换（plain span → doc 落点）"""
    editor.setPlainText("/cmd")
    _type_at_end(editor, " ")
    _type_at_end(editor, "--model=gpt")
    assert editor.toPlainText() == "/cmd --model=gpt"

    editor._on_param_value_selected("gpt-4o")

    # 值不含空格 → 不加引号（含空格才包裹）
    assert editor.toPlainText() == "/cmd --model=gpt-4o "


def test_long_command_name_not_elided(editor):
    """长命令名胶囊不限宽：intrinsicSize 按全名宽度，不截断省略"""
    from PyQt5.QtCore import QSizeF

    from app.widgets.bottom_input_area import CommandMentionObject

    long_name = "super-long-command-name-with-many-words"
    pill = CommandMentionObject()
    fmt = editor._make_command_format(long_name)
    size = pill.intrinsicSize(editor.document(), 0, fmt)

    from PyQt5.QtGui import QFontMetrics

    fm = QFontMetrics(editor.document().defaultFont())
    expected = pill._PAD_LEFT + fm.horizontalAdvance(long_name) + pill._PAD_RIGHT
    assert size == QSizeF(expected, pill._HEIGHT)


def test_tab_silent_after_command_collapsed(editor, monkeypatch):
    """命令已折叠后按 Tab 静默：不重触发补全、不改文本、不重复插参数"""
    fired = []
    editor.slashTriggered.connect(lambda q: fired.append(q))

    editor.setPlainText("/cmd")
    _type_at_end(editor, " ")
    assert _first_frag_is_command_pill(editor)

    from PyQt5.QtCore import QEvent, Qt
    from PyQt5.QtGui import QKeyEvent

    event = QKeyEvent(QEvent.KeyPress, Qt.Key_Tab, Qt.NoModifier)
    editor.keyPressEvent(event)
    assert event.isAccepted()

    assert fired == []
    assert editor.toPlainText() == "/cmd "
    assert _first_frag_is_command_pill(editor)


def test_tab_still_completes_before_collapse(editor, monkeypatch):
    """未折叠阶段 Tab 补全路径保留：/ 开头仍触发 slashTriggered"""
    fired = []
    editor.slashTriggered.connect(lambda q: fired.append(q))
    # Tab 后 10ms 的延迟选中依赖事件循环，测试里只断言信号本身
    monkeypatch.setattr(
        "app.widgets.bottom_input_area.QTimer.singleShot", lambda ms, fn: None
    )

    editor.setPlainText("/cm")
    cursor = editor.textCursor()
    cursor.movePosition(cursor.End)
    editor.setTextCursor(cursor)

    from PyQt5.QtCore import QEvent, Qt
    from PyQt5.QtGui import QKeyEvent

    event = QKeyEvent(QEvent.KeyPress, Qt.Key_Tab, Qt.NoModifier)
    editor.keyPressEvent(event)

    assert fired == ["cm"]

