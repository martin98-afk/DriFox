# -*- coding: utf-8 -*-
"""思考摘要小节标题的解析与渲染回归。

背景：Responses API 的 reasoning summary 没有独立 title 字段，分段标题就是
summary_text 首行的 `**标题**`（ChatGPT / Codex 客户端同款）。渲染层把该行
提到折叠框 summary 行，正文里去掉，避免展开后重复一行带星号的标题。
无标题形态（chat/completions 的 reasoning_content）必须保持旧行为。
"""

from app.widgets.card_render_core import _render_think_block, _split_think_title


def test_split_title_extracts_bold_first_line():
    title, body = _split_think_title("**Using the todo tool**\n\nI think I need to call it.")
    assert title == "Using the todo tool"
    assert body == "I think I need to call it."


def test_split_title_no_title_keeps_content():
    raw = "核对一下状态：三个文件都是 staged。\n\n无待办。"
    title, body = _split_think_title(raw)
    assert title == ""
    assert body == raw


def test_split_title_only_heading():
    """只有标题行（无正文）时正文为空，调用方回退原内容"""
    title, body = _split_think_title("**Plan**")
    assert title == "Plan"
    assert body == ""


def test_split_title_ignores_inline_bold():
    """标题必须是独占首行；行内加粗不算标题"""
    raw = "先看 **重点** 再动手。\n第二行"
    title, body = _split_think_title(raw)
    assert title == ""
    assert body == raw


def test_render_put_title_in_summary_and_strip_from_body():
    html = _render_think_block("**Using the todo tool**\n\n正文内容在这里。", completed=True)
    # 标题进 summary 行
    assert "Using the todo tool" in html
    # 正文里不再出现带星号的标题行
    assert "**Using the todo tool**" not in html
    assert "正文内容在这里。" in html


def test_render_without_title_keeps_preview_behavior():
    html = _render_think_block("核对一下状态：三个文件都是 staged。", completed=True)
    assert "核对一下状态" in html
    assert "**" not in html
