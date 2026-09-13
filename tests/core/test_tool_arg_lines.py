# -*- coding: utf-8 -*-
"""编辑类工具流式行数估算测试（app/core/tool_arg_lines.py）"""

from app.core.tool_arg_lines import (
    build_progress_payload,
    count_escaped_newlines,
    estimate_streaming_lines,
)


def test_counts_real_newline_escapes_only():
    """只数换行转义；字面量反斜杠 n 不算（raw 字符串里 \\n 是反斜杠+n 两字符）"""
    assert count_escaped_newlines(r"a\nb") == 1
    assert count_escaped_newlines(r"a\\nb") == 0
    assert count_escaped_newlines(r"a\\\nb") == 1
    assert count_escaped_newlines("plain") == 0


def test_estimate_write_open_fragment():
    """未闭合的 content 片段：3 行内容（换行 2 + 1）"""
    buf = r'{"path": "a.py", "content": "line1\nline2\nline3'
    assert estimate_streaming_lines("write", buf) == (3, 0)


def test_estimate_edit_both_directions():
    """old=`x\ny` 两行，new=`x\ny\nz\nw` 四行"""
    buf = r'{"path": "a.py", "oldString": "x\ny", "newString": "x\ny\nz\nw'
    assert estimate_streaming_lines("edit", buf) == (4, 2)


def test_estimate_multi_edit_accumulates():
    """old 累计 2+1=3 行，new 累计 1+3=4 行"""
    buf = (
        r'{"path": "a.py", "edits": ['
        r'{"oldString": "a\nb", "newString": "a"}, '
        r'{"oldString": "c", "newString": "c\nd\ne'
    )
    assert estimate_streaming_lines("multi_edit", buf) == (4, 3)


def test_non_edit_tool_returns_zero():
    assert estimate_streaming_lines("read", r'{"path": "a.py", "content": "x\ny') == (0, 0)
    assert estimate_streaming_lines("", "anything") == (0, 0)


def test_build_progress_payload_only_increases():
    """只增不减：本次估出的行数小于上次时沿用上次（防正则失配抖动）"""
    buf = r'{"path": "a.py", "content": "l1\nl2'
    payload, est = build_progress_payload("write", buf, 40, "a.py", (5, 0))
    assert payload["_args_len"] == 40
    assert payload["_path"] == "a.py"
    assert payload["_add_lines"] == 5
    assert est == (5, 0)


def test_build_progress_payload_omits_lines_when_zero():
    payload, est = build_progress_payload("edit", '{"path": "a.py"}', 18, "a.py", (0, 0))
    assert "_add_lines" not in payload and "_del_lines" not in payload
    assert est == (0, 0)
