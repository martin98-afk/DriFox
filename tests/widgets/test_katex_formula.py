"""KaTeX 公式提取测试（设计文档 2026-09-04 §8 测试矩阵）。

对应设计：docs/superpowers/specs/2026-09-04-katex-formula-rendering-design.md
GitHub 规则定界 + CJK 内容收紧 + fence/inline code 保护 + 流式半截安全。
"""

import base64
import re

from app.widgets.message_card import _extract_formulas


def _src(html: str) -> str:
    """取占位标签里的 b64 源码并解码。"""
    m = re.search(r'data-katex-src="([^"]+)"', html)
    assert m, html
    return base64.b64decode(m.group(1)).decode("utf-8")


def test_inline_dollar():
    out = _extract_formulas("质能方程 $E=mc^2$ 很有名")
    assert 'class="katex-inline' in out and "data-katex-src=" in out
    assert _src(out) == "E=mc^2"


def test_display_dollar():
    out = _extract_formulas("$$\\int_0^\\infty e^{-x}dx=1$$")
    assert 'class="katex-block' in out
    assert _src(out) == "\\int_0^\\infty e^{-x}dx=1"


def test_bracket_and_paren():
    assert "data-katex-src" in _extract_formulas(r"\[a+b\]")
    assert "data-katex-src" in _extract_formulas(r"\(a+b\)")


def test_price_not_formula():
    assert "data-katex-src" not in _extract_formulas("$100 和 $200")


def test_shell_var_not_formula():
    assert "data-katex-src" not in _extract_formulas("设 $HOME 后运行")


def test_cjk_content_not_formula():
    assert "data-katex-src" not in _extract_formulas("$美元$")


def test_inline_code_protected():
    assert "data-katex-src" not in _extract_formulas("代码 `$x$` 保持原样")


def test_fence_protected():
    md = "```\n$x$\n```"
    assert "data-katex-src" not in _extract_formulas(md)


def test_escaped_dollar():
    assert "data-katex-src" not in _extract_formulas(r"\$100")


def test_streaming_half():
    md = "$$\\int_0"
    assert _extract_formulas(md) == md  # 原文原样返回


def test_cjk_mixed_line():
    out = _extract_formulas("当 $x \\geq 0$ 时")
    assert "当" in out and _src(out) == "x \\geq 0"


def test_two_formulas_one_line():
    out = _extract_formulas("$$a$$ 中 $$b$$")
    assert out.count("data-katex-src") == 2


def test_markdown_escape_immune():
    out = _extract_formulas("**bold** 与 $a_b^c$ 混排")
    assert _src(out) == "a_b^c"


def test_adjacent_dollars():
    md = "$ $$ $$$"
    assert _extract_formulas(md) == md


def test_pipeline_cached_impl():
    from app.widgets.message_card import _render_markdown_to_html_cached_impl

    html = _render_markdown_to_html_cached_impl("质能 $E=mc^2$ 方程", False)
    assert "data-katex-src" in html
    _render_markdown_to_html_cached_impl.cache_clear()


def test_katex_vendor_files_exist():
    from pathlib import Path

    root = Path(__file__).parents[2]
    vdir = root / "app/resources/web/vendor/katex"
    assert (vdir / "katex.min.js").is_file()
    assert (vdir / "katex.min.css").is_file()
    assert (vdir / "fonts" / "KaTeX_Main-Regular.woff2").is_file()


def test_get_katex_urls_local_first():
    from app.widgets import message_card

    message_card._katex_vendor_urls_cache = None
    css_url, js_url = message_card._get_katex_urls()
    assert css_url.startswith("file://") and js_url.startswith("file://")
    assert message_card._katex_vendor_urls_cache is not None
