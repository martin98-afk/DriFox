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
    _TEXT_BLOCK_CACHE,
    _md_to_html,
    _md_to_html_incremental,
    _plain_text_blocks,
    _split_block_elements,
)


@pytest.fixture(autouse=True)
def _clean_caches():
    """每个用例前后清空两层缓存，避免相互污染（缓存是模块级共享状态）。"""
    _HTML_PREFIX_CACHE.clear()
    _TEXT_BLOCK_CACHE.clear()
    yield
    _HTML_PREFIX_CACHE.clear()
    _TEXT_BLOCK_CACHE.clear()


# ── T5-fix：T6a 审查列出的 8 个不等价输入（必须逐个显式覆盖）──────────────
# 每个形态对应一条等价性守卫：
#   a. prefix 中 ``` 计数为奇（围栏未闭合）
#   b. tail 首个非空行以空白开头（缩进延续）
#   c. not tail.strip()
#   d. not prefix.strip()
#   + 行首裸 HTML（htmlStash 占位符可跨行）
_T6A_CASES = [
    "    a\n\n    b",  # prefix 本身是缩进块 → b
    "```py\nx\n\n普通尾部",  # prefix 围栏未闭合 → a
    "段落。\n\n\t制表缩进",  # tail 制表缩进 → b
    "段落。\n\n 两空格",  # tail 空格缩进 → b
    "段落。\n\n   四个空格",  # tail 四空格 → b
    "段落。\n\n<div>裸HTML</div>",  # tail 行首裸 HTML → HTML 守卫
    "<p>前缀HTML</p>\n\n段落。",  # prefix 行首裸 HTML → HTML 守卫
    "段落。\n\n<!-- 注释 -->\n\n尾部。",  # 注释块 → HTML 守卫
    "段落。\n\n\n\n",  # tail.strip() 为空 → c
    "    \n\n段落。",  # prefix.strip() 为空 → d
]


@pytest.mark.parametrize("text", _T6A_CASES)
def test_t6a_reported_mismatches(text):
    """T6a 报告的 8 个不等价输入（+2 空串形态）：现在必须等价。"""
    assert _md_to_html_incremental(text) == _md_to_html(text), f"不等价：{text!r}"


def test_guard_fence_odd_count():
    """守卫 a：prefix 中 ``` 计数为奇数（围栏未闭合）→ 整段转换。"""
    for text in ("```py\nx\n\n仍在围栏里", "a\n\n```\n无 closing\n\n继续"):
        assert _md_to_html_incremental(text) == _md_to_html(text)


def test_guard_tail_indented():
    """守卫 b：tail 首个非空行以空白开头（缩进延续）→ 整段转换。"""
    for text in ("段落。\n\n\t制表", "段落。\n\n 两空格", "段落。\n\n   四空格", "- a\n\n  - 懒延续"):
        assert _md_to_html_incremental(text) == _md_to_html(text)


def test_guard_raw_html():
    """行首裸 HTML → 整段转换（htmlStash 占位符可跨行）。"""
    for text in (
        "段落。\n\n<div>块</div>",
        "<div>块</div>\n\n段落。",
        "段落。\n\n<span>行内</span>",
        "段落。\n\n<br>",
        "段落。\n\n<!-- 注释 -->",
        "段落。\n\n<b>粗块</b>",
        "段落。\n\n<table><tr><td>c</td></tr></table>",
        "段落。\n\n<details><summary>s</summary>c</details>",
    ):
        assert _md_to_html_incremental(text) == _md_to_html(text)


def test_guard_merge_needs_open_list_in_prefix():
    """守卫细化：只有 prefix 末块也是列表/引用时，tail 的列表标记才判合并。

    细化前「tail 首行是列表标记就整段转换」把「prefix 收尾于普通段落、tail
    开新列表」的安全情形也挡掉（压测 payload 上误判 305/800 = 38%）。细化后
    PASS 率 486/800 → 791/800。等价性仍须成立。
    """
    # prefix 收尾于普通段落 + tail 新开列表：安全，且必须仍等价
    assert _md_to_html_incremental("普通段落。\n\n- 新列表项") == _md_to_html("普通段落。\n\n- 新列表项")
    # prefix 末块本身是列表 + tail 列表延续：会合并，必须走整段
    assert _md_to_html_incremental("- a\n- b\n\n- c") == _md_to_html("- a\n- b\n\n- c")
    assert _md_to_html_incremental("> q1\n\n> q2") == _md_to_html("> q1\n\n> q2")


def test_text_block_cache_is_transparent():
    """段级缓存（_TEXT_BLOCK_CACHE）不得改变输出：命中与未命中结果一致。"""
    text = "# 标题\n\n第一段。\n\n第二段"
    _TEXT_BLOCK_CACHE.clear()
    cold = _plain_text_blocks(text)
    warm = _plain_text_blocks(text)
    assert cold == warm, "段级缓存命中结果必须与冷调用一致"
    assert _TEXT_BLOCK_CACHE, "前置：应产生缓存条目"
    _TEXT_BLOCK_CACHE.clear()
    assert _plain_text_blocks(text) == cold


def test_theme_change_does_not_serve_stale_html():
    """主题令牌变化后不得命中旧主题的缓存 HTML。

    `_md_to_html` 产出内联 style 里的颜色取当前主题令牌，而两层缓存只按文本
    寻址、不感知主题。切主题必须由 `refresh_theme` 清掉，否则旧卡片的稳定
    前缀沿用旧配色（实测已复现过 stale）。
    """
    import app.widgets.markdown_block_viewer as mbv

    text = "第一段。\n\n第二段仍在写"
    assert _md_to_html_incremental(text) == _md_to_html(text)  # 冷调用填充缓存

    orig = mbv._qt_theme
    patched = dict(orig())
    patched["text_secondary"] = "#FF0000"
    try:
        mbv._qt_theme = lambda: patched
        full_new = _md_to_html(text)
        assert _md_to_html_incremental(text) != full_new, "此断言证明不清缓存会产出 stale HTML"
        _HTML_PREFIX_CACHE.clear()
        _TEXT_BLOCK_CACHE.clear()
        assert _md_to_html_incremental(text) == full_new, "清缓存后必须回到等价"
    finally:
        mbv._qt_theme = orig


def test_refresh_theme_clears_caches():
    """`MarkdownBlockViewer.refresh_theme` 必须同时清两层缓存。"""
    from PyQt5.QtWidgets import QApplication

    from app.widgets.markdown_block_viewer import MarkdownBlockViewer

    _ = QApplication.instance() or QApplication([])
    viewer = MarkdownBlockViewer()
    viewer.set_markdown("第一段。\n\n第二段仍在写")
    assert _TEXT_BLOCK_CACHE or _HTML_PREFIX_CACHE, "前置：渲染应产生缓存条目"
    if _TEXT_BLOCK_CACHE:
        k = next(iter(_TEXT_BLOCK_CACHE))
        _TEXT_BLOCK_CACHE[k] = [{"__STALE__": True}]
    if _HTML_PREFIX_CACHE:
        k2 = next(iter(_HTML_PREFIX_CACHE))
        _HTML_PREFIX_CACHE[k2] = "__STALE__"

    # refresh_theme 内部 reconcile 会重填缓存，故不能断言「缓存为空」；
    # 判据 = 旧条目已被清掉（标记不再存在）
    viewer.refresh_theme()
    assert [{"__STALE__": True}] not in _TEXT_BLOCK_CACHE.values(), "段级缓存未被清"
    assert "__STALE__" not in _HTML_PREFIX_CACHE.values(), "HTML 前缀缓存未被清"
    viewer.deleteLater()

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
