# -*- coding: utf-8 -*-
"""编辑类工具流式行数估算测试（app/core/tool_arg_lines.py）"""

from app.core.tools.tool_arg_lines import (
    build_progress_payload,
    count_escaped_newlines,
    estimate_streaming_lines,
    extract_partial_path,
    should_emit_progress,
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


def test_extract_partial_path_tolerates_unclosed():
    """未闭合的 path 片段也要能提取（原正则要求引号闭合，是运行框长时间没文件名的原因）"""
    assert extract_partial_path('{"path": "novel/ch01') == "novel/ch01"
    assert extract_partial_path('{"path": "a.py", "content": "x') == "a.py"
    assert extract_partial_path('{"description": "写第二章", "path": "') == ""
    assert extract_partial_path('{"content": "no path here') == ""
    assert extract_partial_path("") == ""


def test_should_emit_progress_rules():
    """首帧必发；字符涨够且间隔够才发；超保底间隔无条件发"""
    assert should_emit_progress(0, 10, 0.0, 1000.0) is True
    assert should_emit_progress(100, 110, 900.0, 1000.0) is False
    assert should_emit_progress(100, 150, 800.0, 1000.0) is True
    assert should_emit_progress(100, 105, 500.0, 1000.0) is True


def test_build_progress_payload_reuse_lines_skips_scan():
    """reuse_lines=True 沿用上次行数（超长参数下跳过全量扫描，避免 O(n²) 拖住界面）"""
    buf = r'{"content": "a\nb\nc\nd\ne'
    payload, est = build_progress_payload("write", buf, 9999, "", (7, 2), True)
    assert payload["_add_lines"] == 7 and payload["_del_lines"] == 2
    assert est == (7, 2)
    # 同一 buffer 不 reuse 时按实际扫描（content 片段 4 个换行 → 5 行）
    payload2, est2 = build_progress_payload("write", buf, 9999, "", (0, 0), False)
    assert payload2["_add_lines"] == 5 and est2 == (5, 0)
