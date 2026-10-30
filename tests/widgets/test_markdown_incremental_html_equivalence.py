# -*- coding: utf-8 -*-
"""流式增量 HTML 转换的等价性回归（T5 perf 优化的防回归网）。

背景：`_md_to_html_incremental` 为压低流式渲染热路径，把全量 markdown 转换
拆成「已闭合前缀（走缓存）+ 变动尾部（单独转换）」。该优化成立的前提是
**输出与整段转换 byte 级一致**——否则 markdown 结构被悄悄改变（块合并、
分隔符丢失），用户看到的就是渲染回退。

本文件把等价性固化为断言：
1. `_md_to_html_incremental(text) == _md_to_html(text)`（byte 级）
2. 覆盖会破坏等价性的已知边界：列表延续、相邻 blockquote 合并、多空行、
   尾部空白块、缩进末块、围栏未闭、裸 HTML、尾部以 \\n 结尾、无边界
3. 缓存必须感知配置（主题 / 字号），触顶走 LRU 而不是整体 clear
4. 缓存不得串味：同文本二次调用结果一致；清空缓存后仍等价

不 import PyQt5 的部分（纯函数层）无 GUI 依赖；涉及 viewer 的用例单独构造。
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


# ── T6a 审查列出的 8 个不等价输入（必须逐个显式覆盖）──────────────
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

# ── T5-fix2：merge 守卫的「尾部纯空白块」形态 ─────────────────────────
# review 实测 4 形态不等价：prefix 尾部的纯空白块会把列表/引用「截断」的假象
# 造出来，若不向前跳过，就会把同一个列表拆成两个块。
_MERGE_BLANK_CASES = [
    "- x\n\n  \n\n- y",  # 无序列表 + 空格块
    "- x\n\n\t\n\n- y",  # 无序列表 + 制表符块
    "> x\n\n  \n\n> y",  # 引用 + 空格块
    "1. x\n\n  \n\n2. y",  # 有序列表 + 空格块
    "- x\n\n \n\n- y",  # 单空格块
    "> q\n\n   \n\n> q2",  # 引用 + 三空格块
    "> 引用\n\n  \n\n- 列表",  # 引用 + 空白 + 列表（跨类型）
    "- a\n\n  \n\n  - nested",  # 列表 + 空白 + 缩进子项
]


@pytest.mark.parametrize("text", _MERGE_PRONE_PREFIXES)
def test_merge_prone_blocks_stay_whole(text):
    """列表延续 / 相邻 blockquote 合并场景：切分会改结构，必须整段转换。"""
    assert _md_to_html_incremental(text) == _md_to_html(text)


@pytest.mark.parametrize("text", _MERGE_BLANK_CASES)
def test_merge_guard_skips_trailing_blank_blocks(text):
    """merge 守卫必须跳过 prefix 尾部的纯空白块（否则列表被拆、引用断裂）。"""
    assert _md_to_html_incremental(text) == _md_to_html(text), f"不等价：{text!r}"


def test_merge_guard_indented_last_block():
    """prefix 末块本身是缩进块时，后续列表标记也必须判为可合并。

    实测形态：`...\\n\\n\\t制表缩进\\n\\n* 星号` —— 不判可合并会与整段转换不等价。
    """
    for text in (
        "**粗** `码`\n\n\t制表缩进\n\n* 星号\n",
        "段落。\n\n   缩进代码\n\n- 列表",
        "a\n\n\t tab 块\n\n> 引用",
    ):
        assert _md_to_html_incremental(text) == _md_to_html(text), f"不等价：{text!r}"


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
    """块级产物一致：_plain_text_blocks 输出与无缓存实现一致。"""
    for text in (
        "# T\n\n段落\n\n尾部",
        "> q1\n\n尾部",
        "| h|\n|---|\n| a |\n\n尾部".replace("h|", "h |"),
        "a\n\n\n\nb\n\n",
    ):
        assert _md_to_html_incremental(text) == _md_to_html(text)


def test_cache_is_transparent():
    """缓存不得串味：二次调用一致，清空后仍等价。"""
    text = "# 标题\n\n第一段。\n\n第二段还在写"
    first = _md_to_html_incremental(text)
    second = _md_to_html_incremental(text)
    assert first == second == _md_to_html(text)
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


# ── T5-fix2：缓存 key 必须感知配置（主题 / 字号） ─────────────────────
def test_cache_key_includes_config_fingerprint():
    """段级缓存 key 必须带配置指纹，配置变则 key 变（不命中旧 HTML）。

    根因：`_md_to_html` 产出的 inline style 内嵌主题色与字号，而缓存只按文本
    寻址时不感知配置 → 切主题/改字号后命中旧配置的 HTML（实测 stale 复现）。
    """
    import app.widgets.markdown_block_viewer as mbv

    orig = mbv._config_fingerprint
    text = "第一段。\n\n第二段还在写"
    try:
        mbv._config_fingerprint = lambda: "CFG_A"
        first = _md_to_html_incremental(text)
        assert first == _md_to_html(text)
        keys_a = set(_HTML_PREFIX_CACHE)

        mbv._config_fingerprint = lambda: "CFG_B"
        second = _md_to_html_incremental(text)
        keys_b = set(_HTML_PREFIX_CACHE)
        assert keys_b - keys_a, "配置变化必须产生新 key（否则会复用旧配置的 HTML）"
        assert second == _md_to_html(text)
    finally:
        mbv._config_fingerprint = orig


def test_invalidate_font_cache_clears_render_caches():
    """字号/字体变更入口必须清两层渲染缓存。

    走真实的 `invalidate_font_cache()` 路径：它是设置变更回调里的失效入口，
    必须连带清掉内联了字号的 HTML 结果缓存（`_refresh_viewer_font` 是 no-op，
    「QSS 统一管理」对 inline style 不成立）。
    """
    from app.utils.design_tokens import invalidate_font_cache

    text = "字号相关段落。\n\n尾部还在写"
    _md_to_html_incremental(text)
    _plain_text_blocks("另一段用于填 _TEXT_BLOCK_CACHE。")
    assert _HTML_PREFIX_CACHE, "前置：应有缓存条目"
    assert _TEXT_BLOCK_CACHE, "前置：段级缓存应有条目"
    invalidate_font_cache()
    assert not _HTML_PREFIX_CACHE, "invalidate_font_cache 必须清 _HTML_PREFIX_CACHE"
    assert not _TEXT_BLOCK_CACHE, "invalidate_font_cache 必须清 _TEXT_BLOCK_CACHE"


def test_config_fingerprint_tracks_font_size():
    """指纹必须随字号档位变化（**直接调本体，不 patch**）。

    ⚠️ 这条用例的存在本身就是防「测试盲区」：此前用
    `get_ui_font_size()`（返回未缩放的 base，16 个档位全是 14）算指纹，
    去重后只剩 1 个值 → 字号维度完全空转；而当时的用例把 `_config_fingerprint`
    patch 掉了，恰好测不到这个盲区（双重失效）。

    所以这里必须调真实函数，并走真实的配置写入 + 缓存失效链路。
    """
    import app.widgets.markdown_block_viewer as mbv
    from app.utils.config import Settings
    from app.utils.design_tokens import invalidate_font_cache

    cfg = Settings.get_instance()
    orig_key = cfg.ui_font_size.value
    try:
        cfg.ui_font_size.value = "0"
        invalidate_font_cache()
        fp_0 = mbv._config_fingerprint()

        cfg.ui_font_size.value = "10"
        invalidate_font_cache()
        fp_10 = mbv._config_fingerprint()

        assert fp_0 != fp_10, f"字号档位变化必须改变指纹（实际都是 {fp_0!r}）"
    finally:
        cfg.ui_font_size.value = orig_key
        invalidate_font_cache()


def test_config_fingerprint_distinguishes_all_font_size_keys():
    """遍历全部 16 个字号档位：指纹去重后必须 = 16（一档一值）。

    这是 review 的复现方法：若某维度空转，去重数会小于档位数。
    """
    import app.widgets.markdown_block_viewer as mbv
    from app.utils.config import Settings
    from app.utils.design_tokens import FONT_SIZE_OPTIONS, invalidate_font_cache

    cfg = Settings.get_instance()
    orig_key = cfg.ui_font_size.value
    fingerprints = []
    try:
        for key in FONT_SIZE_OPTIONS:
            cfg.ui_font_size.value = key
            invalidate_font_cache()
            fingerprints.append(mbv._config_fingerprint())
    finally:
        cfg.ui_font_size.value = orig_key
        invalidate_font_cache()

    assert len(set(fingerprints)) == len(FONT_SIZE_OPTIONS), (
        f"指纹区分度不足：{len(set(fingerprints))}/{len(FONT_SIZE_OPTIONS)}（字号维度空转？）"
    )


def test_text_block_cache_lru_not_clear_all():
    """段级缓存触顶必须走真 LRU（淘汰最久未用），不能整体 clear。

    整体 clear 会让所有稳定文本段同时失效 → 下一拍全部重算，表现为周期性
    卡顿（真实回复段数可达上百，旧上限 24 时必然频繁触顶）。
    """
    import app.widgets.markdown_block_viewer as mbv

    limit = mbv._TEXT_BLOCK_CACHE_MAX
    assert limit >= 512, f"上限过小（{limit}）会在真实段数下频繁触顶"

    texts = [f"段{i}内容。" for i in range(limit + 20)]
    for t in texts:
        _plain_text_blocks(t)
    assert len(_TEXT_BLOCK_CACHE) == limit, f"应稳定在上限 {limit}，实际 {len(_TEXT_BLOCK_CACHE)}"

    # 触顶后仍能正常等价转换（未被 clear 打乱）
    assert _md_to_html_incremental("段落。\n\n尾部") == _md_to_html("段落。\n\n尾部")


def test_refresh_theme_clears_prefix_cache():
    """`MarkdownBlockViewer.refresh_theme` 必须清两层缓存。"""
    from PyQt5.QtWidgets import QApplication

    from app.widgets.markdown_block_viewer import MarkdownBlockViewer

    _ = QApplication.instance() or QApplication([])
    viewer = MarkdownBlockViewer()
    text = "第一段。\n\n第二段仍在写"
    viewer.set_markdown(text)
    assert _HTML_PREFIX_CACHE, "前置：渲染应产生缓存条目"
    # refresh_theme 内部会 reconcile 重填缓存，故不能断言「缓存为空」；
    # 判据改为「旧条目被清掉」——打标记，标记还在 = 没清。
    for k in list(_HTML_PREFIX_CACHE):
        _HTML_PREFIX_CACHE[k] = "__STALE_MARKER__"
    viewer.refresh_theme()
    assert all(v != "__STALE_MARKER__" for v in _HTML_PREFIX_CACHE.values()), (
        "refresh_theme 必须清空 HTML 前缀缓存（旧主题产物不得残留）"
    )
    viewer.deleteLater()


@pytest.mark.parametrize("text", _T6A_CASES)
def test_t6a_reported_mismatches(text):
    """T6a 报告的 8 个不等价输入（+2 空串形态）：现在必须等价。"""
    assert _md_to_html_incremental(text) == _md_to_html(text), f"不等价：{text!r}"
