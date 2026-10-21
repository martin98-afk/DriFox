# -*- coding: utf-8 -*-
"""输入框参数补全回归测试。"""

from app.widgets.bottom_input_area import SendableTextEdit


def test_single_dash_parameter_completion():
    """单独输入一个参数前缀时，补全应替换前缀而不是追加空格。"""
    editor = SendableTextEdit()
    editor.setPlainText("-")
    editor.moveCursor(editor.textCursor().End)

    editor.insert_parameter_text("--verbose", "flag")

    assert editor.toPlainText() == "--verbose "


def test_double_dash_parameter_completion():
    """已有完整参数前缀时，补全仍应只生成一个参数前缀。"""
    editor = SendableTextEdit()
    editor.setPlainText("--")
    editor.moveCursor(editor.textCursor().End)

    editor.insert_parameter_text("--verbose", "flag")

    assert editor.toPlainText() == "--verbose "
