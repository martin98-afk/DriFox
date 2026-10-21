# -*- coding: utf-8 -*-
"""助手胶囊与 @ 触发检测回归测试。

背景：_on_at_trigger_check 曾把文档坐标的光标位置切在 toPlainText() 的
展开文本上（助手胶囊展开为 "@名字 " 多字符），插入胶囊后每次输入都
从展开文本里扫到幽灵 "@" 误弹 @ 卡片。
"""

import pytest

from app.widgets.bottom_input_area import SendableTextEdit


@pytest.fixture()
def editor(qapp):
    return SendableTextEdit()


def test_pill_insert_does_not_trigger_at(editor):
    """插入助手胶囊（欢迎卡片场景）不应触发 @ 检测"""
    editor.insert_assistant_mention("DriFox", "#7C3AED")
    editor._on_at_trigger_check()
    assert editor._at_trigger_pos == -1


def test_pill_then_typing_does_not_trigger_at(editor):
    """胶囊后继续打字也不应触发（胶囊自带空格曾被坐标错位切掉）"""
    editor.insert_assistant_mention("hanako", "#DB2777")
    cursor = editor.textCursor()
    cursor.movePosition(cursor.End)
    cursor.insertText("继续输入")
    editor.setTextCursor(cursor)
    editor._on_at_trigger_check()
    assert editor._at_trigger_pos == -1


def test_manual_at_still_triggers(editor):
    """胶囊存在时手打 @ 仍应正常触发"""
    editor.insert_assistant_mention("DriFox", "#7C3AED")
    cursor = editor.textCursor()
    cursor.movePosition(cursor.End)
    cursor.insertText("@dv")
    editor.setTextCursor(cursor)
    editor._on_at_trigger_check()
    assert editor._at_trigger_pos >= 0


def test_pill_expands_to_at_name_for_send():
    """发送语义不变：toPlainText 展开胶囊为 @名字（PreUserMessage 依赖）"""
    editor = SendableTextEdit()
    editor.insert_assistant_mention("DriFox", "#7C3AED")
    assert editor.toPlainText() == "@DriFox "
