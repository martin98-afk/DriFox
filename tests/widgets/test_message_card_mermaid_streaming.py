# -*- coding: utf-8 -*-
"""回归测试：流式期间半截 mermaid 触发 error bomb 在消息结尾累积。

现象（用户报告）
----------------
模型流式输出 ```mermaid 块的过程中，消息卡片结尾堆积一串
「Syntax error in text / mermaid version 10.9.1」，图本身最终显示正常。

根因
----
流式中间态 fence 未闭合，_sanitize_incomplete_markdown 补闭合后半截
mermaid 源码照常进入渲染分支；mermaid 10.9.1 render 失败时会把含
「Syntax error in text」的 error bomb SVG 追加到 document.body——它不在
卡片容器内，innerHTML 全量重建清不掉；且占位 div 的 id 是内容 sha1，
半截源码逐轮变化导致 mermaid 同 id 清理机制失效，bomb 逐轮累积。
fence 闭合后图渲染成功，但残留 bomb 无人清理。

修复（两层）
----
1. _sanitize_incomplete_markdown：末尾未闭合 fence 若是 mermaid，语言
   标记改为 mermaid-streaming，本轮降级为普通代码块（源码仍可见），
   渲染选择器 .mermaid-block[data-mermaid-src] 天然跳过，零失败零 bomb。
2. renderMermaidBlocks catch：清理 body 上残留的 error bomb SVG，兜住
   其他失败来源（防线纵深）。
"""

import re

from app.widgets.message_card import (
    _render_markdown_to_html_cached_impl,
    _sanitize_incomplete_markdown,
)

HALF_MERMAID = "前文。\n\n```mermaid\nflowchart TD\n    A[节点] --> B{判断"


def test_streaming_half_mermaid_relabelled():
    """半截 mermaid fence：语言改 mermaid-streaming 并补闭合。"""
    out = _sanitize_incomplete_markdown(HALF_MERMAID)
    assert "```mermaid-streaming" in out, f"语言标记未改: {out!r}"
    assert "```mermaid\n" not in out, "原语言标记残留"
    assert out.count("```") % 2 == 0, "闭合缺失"
    assert "flowchart TD" in out, "半截源码丢失（应保留可见）"


def test_streaming_half_other_lang_untouched():
    """半截非 mermaid 块：只补闭合，不改语言。"""
    src = "```python\nprint(1"
    out = _sanitize_incomplete_markdown(src)
    assert "```python" in out
    assert "mermaid-streaming" not in out
    assert out.count("```") % 2 == 0


def test_complete_mermaid_untouched():
    """已闭合 mermaid：原样透传，不改语言。"""
    src = "```mermaid\nflowchart LR\n    A --> B\n```\n后文"
    assert _sanitize_incomplete_markdown(src) == src


def test_empty_untouched():
    assert _sanitize_incomplete_markdown("") == ""


def test_pipeline_streaming_has_no_render_block():
    """管线级：半截时无渲染块（降级代码块），闭合后有且内容完整。"""
    html_half = _render_markdown_to_html_cached_impl(HALF_MERMAID)
    assert 'data-mermaid-src="' not in html_half, "半截不应下发渲染属性"
    assert "mermaid-streaming" in html_half, "半截应降级为普通代码块"

    complete = HALF_MERMAID + "?}\n    B -- 否 --> A\n```\n\n后文。"
    html_full = _render_markdown_to_html_cached_impl(complete)
    m = re.search(r'data-mermaid-src="([^"]+)"', html_full)
    assert m, "闭合后应有渲染块"
    import base64

    decoded = base64.b64decode(m.group(1)).decode("utf-8")
    assert decoded.startswith("flowchart TD"), "b64 内容失真"


def test_pipeline_mixed_closed_block_not_blocked():
    """同页半截 python 块不连累已闭合 mermaid 的渲染。"""
    md = "```mermaid\nflowchart LR\n    A --> B\n```\n\n```python\nprint(1"
    html = _render_markdown_to_html_cached_impl(md)
    assert 'data-mermaid-src="' in html, "已闭合 mermaid 应正常下发"
