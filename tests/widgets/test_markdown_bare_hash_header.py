# -*- coding: utf-8 -*-
"""回归测试：`#56` 这类裸井号编号被渲染成大标题。

现象（用户报告）
----------------
消息卡片里 `- #57 最重一张已派：…` 变成占满宽度的巨型标题：字号暴涨、原始
`#` 消失、列表项里还嵌了一层 `<h1>`。同一段里 `- **#56 …**` 却正常（`**`
挡住了 `#`），所以看起来像「有的行坏了有的行没坏」。

根因
----
Python-Markdown 的 ATX 标题规则**不要求** `#` 后必须有空格（CommonMark 要求），
于是行首的 `#56 审计…`、`#T1 完成…`、`##状态盘点` 全被判成 `<h1>`/`<h2>`。
本机会话库统计：行首裸井号 1700+ 处（`#56` 编号 1511 / `#T1` 167 /
`##状态盘点` 57），是正文里最主要的噪源。

修复
----
_sanitize_incomplete_markdown 入口处调用 _escape_bare_hash_headers：把
「`#` 后紧跟非空白字符」的裸井号转义成字面 `#`。该函数是所有渲染管线
（全量 / 差量段 / 尾部行内 / worker / viewer / block）的共同入口，一处补齐全线生效。
"""

from app.widgets.card_render_core import (
    _escape_bare_hash_headers,
    _render_markdown_to_html_cached_impl,
    _sanitize_incomplete_markdown,
)

# ===== 正文里当字面量的裸井号：必须渲染成普通文本 =====
BARE_HASHES = [
    "#56 审计⑧判「可继续」",  # 编号在行首
    "- #57 最重一张已派：#32 甲专项",  # 无序列表项
    "* #57 星号列表",
    "+ #57 加号列表",
    "1. #42 vision 补审",  # 有序列表项
    "  - #57 两空格缩进的列表",
    "> #56 引用里的编号",
    "##状态盘点",  # 无空格的二级井号
    "#T1 完成，基线报告已发",  # 字母编号
    "#10 基线快照 + #6 的 P2 已交付",  # 一行多个编号
]

# ===== 真标题：带空格，必须照旧渲染 =====
REAL_HEADERS = [
    ("# 一级标题", "<h1>"),
    ("## 二级标题", "<h2>"),
    ("###### 六级标题", "<h6>"),
]


def test_bare_hash_not_rendered_as_header():
    """裸井号编号一律不进标题标签，且 `#` 保留在正文里。"""
    for src in BARE_HASHES:
        html = _render_markdown_to_html_cached_impl(src, compact=False)
        assert "<h1>" not in html and "<h2>" not in html, f"被当成标题: {src!r} → {html[:120]}"
        # 原文井号必须还在（转义不删字符）
        assert "#" in html, f"井号被吃掉: {src!r} → {html[:120]}"


def test_bare_hash_keeps_list_structure():
    """列表项里的编号不能让 `<li>` 里再嵌一层标题（截图里的错乱形态）。"""
    html = _render_markdown_to_html_cached_impl("- #57 最重一张已派", compact=False)
    assert "<ul>" in html and "<li>" in html
    assert "<h1>" not in html
    assert "#57" in html


def test_real_headers_unaffected():
    """带空格的 `#` 仍是真标题。"""
    for src, tag in REAL_HEADERS:
        html = _render_markdown_to_html_cached_impl(src, compact=False)
        assert tag in html, f"真标题失效: {src!r} → {html[:120]}"


def test_code_blocks_untouched():
    """围栏与缩进代码块里的 `#` 原样保留（`#include` 不能被改）。"""
    fenced = "```\n#include <stdio.h>\n#56 代码注释\n```"
    html = _render_markdown_to_html_cached_impl(fenced, compact=False)
    assert "\\#" not in html
    assert "#include" in html and "#56 代码注释" in html

    indented = "    #56 缩进代码"
    html = _render_markdown_to_html_cached_impl(indented, compact=False)
    assert "\\#" not in html


def test_inline_and_midline_hash_untouched():
    """行内代码与段落中间的井号不受影响。"""
    for src in ("正文 #56 中间的井号", "`#56` 行内代码"):
        assert _escape_bare_hash_headers(src) == src


def test_escape_is_idempotent():
    """重复调用不叠加转义（缓存重入 / 多次渲染路径安全）。"""
    for src in BARE_HASHES + ["# 真标题", "```\n#56\n```"]:
        once = _escape_bare_hash_headers(src)
        assert _escape_bare_hash_headers(once) == once, f"非幂等: {src!r}"


def test_sanitize_entry_wires_escape():
    """_sanitize_incomplete_markdown 是共同入口，必须带上转义。"""
    out = _sanitize_incomplete_markdown("#56 审计判定")
    assert out.startswith("\\#56"), out

def test_style_block_untouched():
    """`<style>` / `<script>` 块内的行首 `#` 不能转义：ID 选择器被改成
    `\\#tu-tip` 后 CSS 解析成元素名选择器（永不匹配）→ 自绘 tooltip 的
    display:none / position:fixed 全失效，文本以正文形式堆在卡片末尾。
    """
    css = "<style>\n#tu-tip {\n  display: none;\n}\n</style>"
    out = _escape_bare_hash_headers(css)
    assert "\\#tu-tip" not in out, out
    assert "#tu-tip" in out
    script = "<script>\n#anchor\n</script>"
    assert "\\#anchor" not in _escape_bare_hash_headers(script)
    # 块外的正文裸井号仍要转义
    assert "\\#56" in _escape_bare_hash_headers(css + "\n#56 正文编号")
    # 围栏里出现半个 <style> 不能吞掉后续转义
    assert _escape_bare_hash_headers("```\n<style>\n```\n#8 z").rstrip().endswith("\\#8 z")


def test_style_block_css_survives_pipeline():
    """整条渲染管线：插件片段里的 ID 选择器必须原样落地。"""
    html = _render_markdown_to_html_cached_impl(
        "<style>\n#tu-tip {\n  display: none; position: fixed;\n}\n</style>", compact=False
    )
    assert "#tu-tip {" in html and "\\#tu-tip" not in html, html[-400:]
