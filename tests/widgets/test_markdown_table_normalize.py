# -*- coding: utf-8 -*-
"""回归测试：缺少空行的 markdown 表格渲染成普通文本（表格不出来）。

现象（用户报告）
----------------
消息卡片里模型输出的表格经常不渲染成表格，整张表退化成一行行带 `|`
的普通文字（用 <br> 连成一段），截图里表格头 `| 项 | 内容 | 实测收益 |`
和分隔行 `|---|---|---|` 原样露出。

根因
----
模型常写成「段落文字下一行直接跟表格」，中间不给空行。Python-Markdown 的
tables 扩展只在**独立块**上生效（BlockProcessor.test 拿到的 block 必须以
表格头行开头），GFM 规范同样不允许表格打断段落。缺空行时整张表退化成
带 `|` 的段落文本。

修复
----
_sanitize_incomplete_markdown 入口处调用 _normalize_table_blocks：给缺空行
的表格补前后空行。该函数是所有渲染管线（全量 / 差量段 / 尾部行内 / worker /
viewer / block）的共同入口，一处补齐全线生效。
"""

from app.widgets.card_render_core import (
    _normalize_table_blocks,
    _render_markdown_to_html_cached_impl,
)

# 用户报告的真实形态：段落紧跟表格、表格紧跟正文，全程无空行
NO_BLANK_TABLE = """已落地（全过四件套，每项带独立回滚 patch）
| 项 | 内容 | 实测收益 |
|---|---|---|
| #13 | tool_prune split 复用 | 探测内存 -43% |
| #22 | 三仓储 DDL 门控 | DDL 15→1 |
在途：#33 丙、#37 S8 护栏测试。
"""


def test_table_glued_to_paragraph_renders():
    """表格紧贴上一段正文（无空行）也必须渲染成 <table>。"""
    html = _render_markdown_to_html_cached_impl(NO_BLANK_TABLE)
    assert "<table" in html, f"表格未渲染：{html[:400]}"
    assert "<th>" in html and "<td>" in html
    # 表头与两条数据行各占一个 <tr>
    assert html.count("<tr>") == 3, f"行数不符：{html.count('<tr>')}"


def test_text_after_table_not_swallowed_as_row():
    """表格下方紧跟的正文不得被 tables 扩展吞成缺列的数据行。"""
    html = _render_markdown_to_html_cached_impl(NO_BLANK_TABLE)
    assert "在途：#33" in html
    # 正文若被吞成表格行会出现在 <td> 里
    assert "在途：#33 丙、#37 S8 护栏测试。</td>" not in html


def test_normalize_is_idempotent():
    """已经规范（表格前后有空行）的输入不产生任何改动。"""
    once = _normalize_table_blocks(NO_BLANK_TABLE)
    assert _normalize_table_blocks(once) == once


def test_normalize_keeps_fenced_table_untouched():
    """``` 围栏内的表格文本是示例源码，不得插入空行。"""
    src = "说明\n```\n| a | b |\n|---|---|\n|1|2|\n```\n"
    assert _normalize_table_blocks(src) == src


def test_normalize_ignores_single_pipe_line():
    """只有一行的 `a | b` 不是表格，不能触发补空行。"""
    src = "上一段\na | b 只是普通文本\n"
    assert _normalize_table_blocks(src) == src


def test_normalize_handles_alignment_separator():
    """对齐语法分隔行（:--- / :---: / ---:）同样识别为表格。"""
    src = "前文\n| a | b | c |\n| :--- | :---: | ---: |\n|1|2|3|\n"
    html = _render_markdown_to_html_cached_impl(src)
    assert "<table" in html
    assert 'style="text-align: center;"' in html or "text-align" in html


def test_table_after_list_renders():
    """列表项紧跟表格（无空行）也要切开，否则表格行被吞进 <li>。"""
    src = "- 条目\n| a | b |\n|---|---|\n|1|2|\n"
    html = _render_markdown_to_html_cached_impl(src)
    assert "<table" in html
    assert "<li>条目" in html
