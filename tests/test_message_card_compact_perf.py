# -*- coding: utf-8 -*-
"""
MessageCard 简洁模式渲染基线测试。

目的：
- 保护 ``_render_markdown_to_html_cached_impl`` 在 ``compact=True`` 时生成的
  HTML 关键不变量（think-compact 纯文本行、tool-block 折叠框、编辑类工具块不被迁移）
- 给出长会话场景的渲染耗时基线，避免后续修改意外引入性能回归

关联变更：
- 简洁模式（ui_compact_tool_area）下 ``MessageCard.reorganizeContent`` 改为
  单次扫描 + 顺序哈希 diff（2026-07-31）
- 简洁模式 ``updateContent`` 跳过 .think-block 展开状态 save/restore
"""

import time

from app.widgets.message_card import _render_markdown_to_html_cached_impl


def _gen_long_md(num_tool_blocks: int = 50, num_think_blocks: int = 50) -> str:
    """构造含多个 think + tool 块的长 markdown 文本。"""
    parts = ["# 测试标题", "\n正文段落。"]
    for i in range(num_think_blocks):
        parts.append(f"<think>思考轮次 {i}：{'x' * 200}</think>")
    for i in range(num_tool_blocks):
        parts.append(
            "<tool>\n"
            f"name: test_tool\n"
            f"args: {{\"i\": {i}, \"text\": \"payload-{i}\"}}\n"
            f"result: result_{i}\n"
            "success: true\n"
            f"tool_call_id: tc-{i}\n"
            "</tool>"
        )
    return "\n".join(parts)


def test_compact_think_block_uses_think_compact_class():
    """简洁模式下完成的思考块应是 think-compact 纯文本行，不是折叠框。"""
    md = "<think>详细思考内容" + "x" * 300 + "</think>\n正文段落"
    html = _render_markdown_to_html_cached_impl(md, compact=True)
    assert "think-compact" in html, "简洁模式应生成 think-compact 纯文本行"
    assert "cm-collapsible think-block" not in html, "简洁模式不应生成折叠框形态的 think-block"


def test_compact_tool_block_uses_collapsed_default():
    """简洁模式下 tool-block 默认折叠（与当前实现一致）。"""
    md = (
        "<tool>\nname: test_tool\n"
        "args: {\"k\": \"v\"}\n"
        "result: r\nsuccess: true\n"
        "tool_call_id: tc-1\n</tool>"
    )
    html = _render_markdown_to_html_cached_impl(md, compact=True)
    assert "cm-collapsible tool-block" in html
    assert "data-block-key" in html
    assert 'data-expanded="false"' in html


def test_compact_long_session_renders_within_budget():
    """100 块混合内容应在 1.5s 内渲染完成（性能基线）。"""
    md = _gen_long_md(num_tool_blocks=50, num_think_blocks=50)
    t0 = time.perf_counter()
    html = _render_markdown_to_html_cached_impl(md, compact=True)
    elapsed = time.perf_counter() - t0
    assert "think-compact" in html
    assert "tool-block" in html
    # 宽松阈值：基线保护，避免后续修改意外退化为 N² 行为
    assert elapsed < 1.5, f"长会话渲染耗时 {elapsed:.2f}s 超过 1.5s 阈值"


def test_non_compact_think_block_uses_collapsible():
    """非简洁模式下完成的思考块应是折叠框（think-block + cm-collapsible）。"""
    md = "<think>详细思考内容" + "x" * 300 + "</think>\n正文段落"
    html = _render_markdown_to_html_cached_impl(md, compact=False)
    assert "cm-collapsible think-block" in html, "非简洁模式应生成折叠框形态"
    assert "think-compact" not in html, "非简洁模式不应生成 think-compact"
