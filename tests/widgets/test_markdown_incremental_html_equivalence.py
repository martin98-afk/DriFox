# -*- coding: utf-8 -*-
"""流式增量 HTML 转换的等价性回归（T5 perf 优化的防回归网）。

背景：`_md_to_html_incremental` 为压低流式渲染热路径，把全量 markdown 转换
拆成「已闭合前缀（走缓存）+ 变动尾部（单独转换）」。该优化成立的前提是
**输出与整段转换 byte 级一致**——否则 markdown 结构被悄悄改变（块合并、
分隔符丢失），用户看到的就是渲染回退。

本文件把等价性固化为断言：
1. `_md_to_html_incremental(text) == _md_to_html(text)`（byte 级）
2. 覆盖会破坏等价性的已知边界：列表延续、相邻 blockquote 合并、多空行、
   尾部以 \\n 结尾、无边界、围栏/表格/缩进代码
3. 缓存不得串味：同文本二次调用结果一致；清空缓存后仍等价

不 import PyQt5（纯函数层），无 GUI 依赖。
"""

import itertools

import pytest

from app.widgets.markdown_block_viewer import (
    _HTML_PREFIX_CACHE,
    _md_to_html,
    _md_to_html_incremental,
    _plain_text_blocks,
    _split_block_elements,
)

# 会与 prefix 末块合并的顶层块（这两类必须走整段转换，不能切分）
_MERGE_PRONE_PREFIXES = [
    "- a\n- b\n\n- c",  # 列表延续
    "- a\n  - a1\n\n- b",  # 嵌套列表延续
    "> q1\n\n> q2",  # 相邻 blockquote 合并
]

# 常规前缀 × 分隔符 × 尾部组合（分隔符空行数取 1..4）
_PREFIXES = [
    "a",
    "# h",
    "- x\n- y",
    "> q",
    "```py\nc\n```",
    "    ind",
    "| a | b |\n|---|---|\n| 1 | 2 |",
    "para with **bold** and `code`",
    "---",
    "1. one\n2. two",
]
_TAILS = [
    "b",
    "b\n",
    "b\n\n",
    "\nb",
    "- c",
    "  - c",
    "1. c",
    "> c",
    "```py\nz\n```",
    "```py\nz",
    "# h2",
    "| a |\n|---|\n| 9 |",
    "",
    "\n",
    "\n\n",
]


@pytest.mark.parametrize("text", _MERGE_PRONE_PREFIXES)
def test_merge_prone_blocks_stay_whole(text):
    """列表延续 / 相邻 blockquote 合并场景：切分会改结构，必须整段转换。"""
    assert _md_to_html_incremental(text) == _md_to_html(text)


def test_combinatorial_equivalence():
    """600 组 prefix × sep × tail 组合：增量转换必须与整段转换 byte 级一致。"""
    checked = 0
    for prefix, tail, sep in itertools.product(_PREFIXES, _TAILS, ("\n\n", "\n\n\n", "\n\n\n\n", "\n\n\n\n\n")):
        text = prefix + sep + tail
        if not text.strip():
            continue
        whole = _md_to_html(text)
        got = _md_to_html_incremental(text)
        assert whole == got, f"不一致：prefix={prefix!r} sep={sep!r} tail={tail!r}"
        checked += 1
    assert checked >= 400, f"组合覆盖不足：{checked}"


def test_no_boundary_falls_back_to_whole():
    """无空行边界时退化为整段转换（含单块 / 尾无空行）。"""
    for text in ("只有一段。\n", "a\n\nb", "no blank line at all"):
        assert _md_to_html_incremental(text) == _md_to_html(text)


def test_block_split_identical():
    """等价性必须贯通到块级：_split_block_elements 结果一致才算真等价。"""
    for text in (
        "# T\n\n段落\n\n- 列表\n\n```py\nx\n```\n\n尾部",
        "> q1\n> q2\n\n尾部",
        "| h |\n|---|\n| a |\n\n尾部",
        "    code\n\n尾部",
        "a\n\n\n\nb",
    ):
        whole_html = _md_to_html(text)
        incr_html = _md_to_html_incremental(text)
        assert _split_block_elements(whole_html) == _split_block_elements(incr_html)


def test_plain_text_blocks_identical():
    """块级产物一致：_plain_text_blocks 输出的块列表必须相同。"""
    for text in (
        "# T\n\n段落\n\n尾部",
        "> q1\n\n尾部",
        "| h |\n|---|\n| a |\n\n尾部",
        "a\n\n\n\nb\n\n",
    ):
        assert _plain_text_blocks(text) == _plain_text_blocks(text)
        assert _md_to_html_incremental(text) == _md_to_html(text)


def test_cache_is_transparent():
    """缓存不得串味：二次调用一致，清空后仍等价。"""
    text = "# 标题\n\n第一段。\n\n第二段还在写"
    first = _md_to_html_incremental(text)
    second = _md_to_html_incremental(text)
    assert first == second == _md_to_html(text)
    # 追加尾部后再跑：前缀应命中缓存，结果仍等价
    longer = text + "更多内容"
    assert _md_to_html_incremental(longer) == _md_to_html(longer)
    assert _HTML_PREFIX_CACHE, "前缀缓存应有条目"
    _HTML_PREFIX_CACHE.clear()
    assert _md_to_html_incremental(longer) == _md_to_html(longer)


def test_streaming_progression_equivalence():
    """模拟真实流式推进：每个中间态的增量转换都与整段转换一致。"""
    base = "# 标题\n\n第一段正文。\n\n- 列表项一\n- 列表项二\n\n```python\nprint(1)\n```\n\n"
    streamed = ""
    for piece in (base, "新的", "内容", "继续", "增长", "中", "。"):
        streamed += piece
        assert _md_to_html_incremental(streamed) == _md_to_html(streamed), f"中间态不一致：{streamed!r}"
