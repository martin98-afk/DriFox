# -*- coding: utf-8 -*-
"""回归测试：多行内联 SVG 被 Python-Markdown 撕碎导致消息卡片画不出来。

现象（用户报告）
----------------
模型按 visualization 协议内联输出多行 <svg>（```svg 围栏之外的裸 SVG）时，
消息卡片不渲染图形。

根因
----
Python-Markdown 对多行 raw HTML 中的块级子元素（<style>/<defs> 等）按
段落边界处理：SVG 树被腰斩成多个 <p> 碎片，nl2br 还在 SVG 行间插 <br>。
产物 HTML 结构损坏，Chromium 重组后无法渲染。

修复
----
_protect_inline_svg_blocks 在 markdown convert 前把独立成段的多行内联
SVG 包进块级 <div>（前后补空行），Python-Markdown 对块级容器内部原样
保留，SVG 结构完整透传。```svg 围栏内的行不走包裹（由
_wrap_code_blocks_with_copy_button_web 的透传分支处理）。
"""

from app.widgets.message_card import (
    _protect_inline_svg_blocks,
    _render_markdown_to_html_cached_impl,
    _render_stable_segment,
)

SVG_MULTI = (
    '<svg viewBox="0 0 680 416" width="100%" role="img" xmlns="http://www.w3.org/2000/svg">\n'
    "<title>鹈鹕</title>\n"
    "<style>\n"
    ".peli-note{animation:peli-f 1.6s ease-in-out infinite}\n"
    "</style>\n"
    "<defs>\n"
    '<linearGradient id="g1"><stop offset="0" stop-color="#FFF"/></linearGradient>\n'
    "</defs>\n"
    '<circle cx="262" cy="300" r="74" fill="none" stroke="#2C2C2A" stroke-width="8"/>\n'
    "</svg>"
)

SVG_ONE_LINE = (
    '<svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">'
    '<rect width="200" height="100" fill="steelblue"/></svg>'
)


def test_protect_wraps_multiline_inline_svg():
    """多行内联 SVG 被包进块级 div，前后补空行。"""
    src = f"前文\n\n{SVG_MULTI}\n\n后文"
    out = _protect_inline_svg_blocks(src)
    assert "<div>\n<svg" in out, f"多行 SVG 未被 div 包裹: {out[:200]!r}"
    assert "</svg>\n</div>" in out, f"div 闭合缺失: {out[-200:]!r}"
    assert "前文" in out and "后文" in out


def test_protect_skips_fence_lines():
    """``` 围栏内的 SVG 行不被误包（围栏由 wrap 透传分支处理）。"""
    src = f"```svg\n{SVG_MULTI}\n```\n\n正文"
    out = _protect_inline_svg_blocks(src)
    assert "<div>" not in out, f"围栏内 SVG 被误包: {out!r}"
    assert "```svg" in out


def test_protect_keeps_unclosed_svg_as_is():
    """流式中间态的未闭合 SVG 保持原样，不包裹不崩。"""
    src = '前文\n\n<svg viewBox="0 0 10 10">\n<circle cx="5"'
    out = _protect_inline_svg_blocks(src)
    assert "<div>" not in out
    assert '<circle cx="5"' in out


def test_protect_noop_without_svg():
    """无 SVG 的文本原样返回（含 <svg 字样的纯文本也不误伤结构）。"""
    src = "普通文本\n\n第二段"
    assert _protect_inline_svg_blocks(src) == src


def test_multiline_inline_svg_survives_full_render():
    """端到端：多行内联 SVG 经全量渲染管线后结构完整（原 bug 场景）。"""
    html = _render_markdown_to_html_cached_impl(f"前文\n\n{SVG_MULTI}")
    assert "<p><svg" not in html, "SVG 开标签被段落包裹（撕碎特征）"
    assert "</svg></p>" not in html, "SVG 闭标签被段落包裹（撕碎特征）"
    svg_start = html.find("<svg")
    svg_end = html.find("</svg>")
    assert svg_start != -1 and svg_end != -1
    assert "<br>" not in html[svg_start:svg_end], "SVG 内部被 nl2br 插入 <br>"
    assert '<circle cx="262"' in html, "SVG 子元素丢失"


def test_diff_render_multiline_inline_svg():
    """差量渲染路径同样透传多行内联 SVG。"""
    html = _render_stable_segment(f"前文\n\n{SVG_MULTI}", compact=False)
    assert "<p><svg" not in html
    assert "</svg>" in html
    assert '<circle cx="262"' in html


def test_single_line_inline_svg_still_inline():
    """单行内联 SVG（原本就正常）不回归。"""
    html = _render_markdown_to_html_cached_impl(f"前文\n\n{SVG_ONE_LINE}\n\n后文")
    assert "<svg" in html and "&lt;svg" not in html


def test_fence_svg_still_passthrough():
    """```svg 围栏走 wrap 透传分支（既有多行也能渲染），不回归。"""
    html = _render_markdown_to_html_cached_impl(f"```svg\n{SVG_MULTI}\n```\n\n正文")
    assert "&lt;svg" not in html
    assert "code-container" not in html
    assert "</svg>" in html
    assert "正文" in html


def test_multiple_svg_blocks():
    """同一条消息多个多行 SVG 块各自完整。"""
    html = _render_markdown_to_html_cached_impl(f"一\n\n{SVG_MULTI}\n\n二\n\n{SVG_MULTI}")
    assert html.count("</svg>") == 2
    assert html.count('<circle cx="262"') == 2
