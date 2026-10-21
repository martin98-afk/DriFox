# -*- coding: utf-8 -*-
"""回归测试：未闭合 <tool> 块以裸文本泄漏到正文（工具结果源码泄露）。

现象（用户报告，2026-09-05 截图）
----------------
简洁模式流式对话中，正文区出现 `name: bash args: {...} result: ...` 的
工具调用/结果序列化原文；工具卡同时正常出现在"工具与思考"折叠区。

根因
----
`_inject_tool_blocks` 未闭合分支把 `<tool>...`（无 `</tool>`）原样保留进
`md.convert` → 浏览器把 `<tool>` 当未知 HTML 元素（标签本身不可见），
内部 name:/args:/result: 字段文本裸露为正文。

未闭合块来源：模型在正文中输出协议格式文本（讨论工具调用机制时模仿
上下文中的序列化格式，流式中 `</tool>` 未到达）、max_tokens 截断、
停止生成。

修复
----
- 未闭合块按到达程度渲染：流式中间态 → 运行中占位框（tool-streaming-block，
  与 JS 注入运行框视觉一致）；非流式终态（截断/停止）→ 容错解析已有字段
  渲染完成卡。不再让标签进 DOM 结构。
- 容错解析范围截断到下一个 <tool> 开标签，剩余段递归处理（审查 I-1：
  防嵌套/后续块字段混入本块容错解析吞正文）。
- 孤立 `</tool>`（无 `<tool>` 开头）：清理，对齐 `_inject_think_cards`
  对孤立 `</think>` 的防御。
- `_render_markdown_to_html` 同步版流式分支补 fence 保护（对齐 worker
  版），fence 内协议示例不再被抽成假卡片。
"""

import re

from app.widgets.message_card import (
    _extract_closed_segments,
    _extract_fenced_code,
    _inject_tool_blocks,
    _render_stable_segment,
    _restore_fenced_code,
    _sanitize_incomplete_markdown,
    _unwrap_code_blocks_with_context_links,
    _inject_context_links,
    _inject_think_cards,
    _render_tool_streaming_block,
    get_markdown_instance,
)

# 字段文本行首模式（审查 S-3：不绑定具体工具名/命令内容，
# 任何工具的协议字段裸泄都命中）
_LEAK_FIELD_PATTERN = re.compile(
    r"(^(?:name|args|result|success|tool_call_id):\s)" r"|(^\d+:(?:from|import|def|class)\s)", re.MULTILINE
)

TOOL_MD_TRUNC = (
    "<tool>\n"
    "name: bash\n"
    'args: {"description": "查类定义", "command": "findstr /n \\"class X\\" w.py"}\n'
    "result: 14:from PyQt5.QtCore import QObject\n"
    "29:# ===== 性能优化 =====\n"
    '30:_THINKING_PATTERN = re.compile(r"'
)


def _assert_no_leak(tag: str, html: str):
    hit = _LEAK_FIELD_PATTERN.search(html or "")
    assert not hit, f"{tag}: 工具源码字段泄漏到 HTML: {hit.group(0)!r}\n{(html or '')[:400]!r}"


def _convert(md: str) -> str:
    mi = get_markdown_instance()
    mi.reset()
    return mi.convert(md)


# ── 未闭合块：流式态渲染运行中占位框，字段文本不得裸泄 ──
def test_unclosed_tool_block_no_leak_streaming():
    out = _inject_tool_blocks("正文。\n\n" + TOOL_MD_TRUNC, False, compact=True)
    assert "tool-streaming-block" in out, f"流式未闭合块应渲染运行中占位框: {out[:300]!r}"
    _assert_no_leak("inject输出", _convert(out))


# ── 未闭合块：非流式终态（截断/停止）容错解析渲染完成卡，不裸泄 ──
def test_unclosed_tool_block_no_leak_final():
    out = _inject_tool_blocks("正文。\n\n" + TOOL_MD_TRUNC, True, compact=True)
    # 容错解析渲染完成态工具卡（无裸 <tool> 标签、无结构化裸文本）
    assert "<tool>" not in out, f"未闭合块不得残留裸 <tool> 标签: {out[:300]!r}"
    _assert_no_leak("inject输出(非流式)", _convert(out))


# ── 孤立闭合标签：清理，不透传 ──
def test_orphan_close_tag_removed():
    out = _inject_tool_blocks("正文提及 </tool> 标签", False, compact=True)
    assert "</tool>" not in out, f"孤立 </tool> 应被清理: {out!r}"
    assert "正文提及" in out


# ── 完整块：不受修复影响，正常抽出渲染 ──
def test_closed_tool_block_still_renders_card():
    md = '正文。\n\n<tool>\nname: bash\nargs: {"command": "ls"}\nresult: file_a\nsuccess: True\n</tool>'
    out = _inject_tool_blocks(md, True, compact=True)
    assert "<tool>" not in out, f"完整块应被抽出: {out!r}"
    assert "cm-collapsible" in out or "tool" in out.lower(), f"完整块应渲染工具卡: {out[:300]!r}"
    assert "正文。" in out


# ── 差量路径：未闭合块仍不进闭合段（既有守卫不回归）──
def test_closed_segments_skip_unclosed_tool():
    stable, segs = _extract_closed_segments("正文段。\n\n<tool>\nname: bash\nresult: x")
    for seg in segs:
        assert "<tool>" not in seg, f"未闭合块不得进闭合段: {seg!r}"


def test_render_stable_segment_no_leak():
    html = _render_stable_segment("<tool>\nname: bash\nresult: x</tool>", compact=True)
    _assert_no_leak("stable_segment(完整块)", html)


# ── 审查 I-1：未闭合段内嵌套/后续 <tool> 开标签不得混入本块容错解析 ──
def test_unclosed_block_truncates_at_next_open_tag():
    md = "<tool>\nname: bash\nresult: 半截\n\n<tool>\nname: read\nresult: y"
    out = _inject_tool_blocks(md, True, compact=True)
    # 两个块各自独立渲染（第二个块字段不混入第一个块）
    assert out.count("data-tool-name") == 2, f"应产出两张容错卡: {out[:400]!r}"
    assert 'data-tool-name="bash"' in out and 'data-tool-name="read"' in out, f"块名应各自识别: {out[:400]!r}"
    assert out.count("<tool>") == 0, f"不得残留裸标签: {out[:400]!r}"
    _assert_no_leak("inject输出(嵌套)", _convert(out))


# ── fence 相邻场景（审查建议正式化）：相邻 fence + fence 与 tool 块相邻 ──
def _fence_guard_pipeline(md: str, completed: bool) -> str:
    """与 _render_markdown_to_html 流式分支同构的管线序列。"""
    safe_md = _sanitize_incomplete_markdown(md)
    safe_md = _unwrap_code_blocks_with_context_links(safe_md)
    safe_md = _inject_context_links(safe_md)
    _fences, safe_md = _extract_fenced_code(safe_md)
    processed = _inject_think_cards(safe_md, completed, compact=True)
    processed = _inject_tool_blocks(processed, completed, compact=True)
    return _restore_fenced_code(processed, _fences)


def test_fence_guard_adjacent_fences_no_fake_card():
    """相邻 fence（```xml 与 ```html 紧邻）内协议标签不得被抽成假卡。"""
    md = (
        "示例一：\n\n```xml\n<tool>\nname: bash\nresult: x\n</tool>\n```\n\n"
        "示例二：\n\n```html\n<div><tool>name: ls</tool></div>\n```"
    )
    mi = get_markdown_instance()
    mi.reset()
    html = mi.convert(_fence_guard_pipeline(md, False))
    assert 'data-tool-name="bash"' not in html, f"fence 内示例被抽成假卡:\n{html[:400]!r}"
    assert "&lt;tool&gt;" in html, f"fence 内协议标签应字面显示:\n{html[:400]!r}"


def test_fence_guard_tool_block_after_fence_still_extracted():
    """fence 之后的真 tool 块（fence 外）仍正常抽出，不受 fence 保护误伤。"""
    md = (
        "```python\ncode = 1\n```\n\n"
        '<tool>\nname: bash\nargs: {"command": "ls"}\nresult: file_a\nsuccess: True\n</tool>'
    )
    out = _fence_guard_pipeline(md, True)
    assert "<tool>" not in out, f"fence 外完整块应被抽出: {out[:400]!r}"
    assert 'data-tool-name="bash"' in out, f"fence 外完整块应渲染工具卡: {out[:400]!r}"
    assert "<pre" in out, f"fence 代码块应保留: {out[:400]!r}"
