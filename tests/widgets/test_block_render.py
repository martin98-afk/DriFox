# -*- coding: utf-8 -*-
"""block 级渲染管线（app/widgets/render）的纯函数测试。

覆盖三条契约：

1. **等价**：同一份内容块，走新 block 管线与走旧整串管线，可见文本一致。
   这是步骤 1 的验收标准 —— 新层先在测试里跑通，再接调用点。
2. **增量最小**：流式追加一批文本时，patch 只替换一个节点的内容，
   不触碰任何历史块（旧管线的「猜稳定区」就是在做这件事的替代品）。
3. **归属正确**：工具 / 思考块的 ``home`` 决定它进正文还是「工具与思考」区，
   取代 JS 侧 ``reorganizeContent`` 的事后搬移。

不需要 QApplication：本层纯字符串计算，不构造任何 QWidget。
"""

from __future__ import annotations

import pytest

from app.core.conversation.message_content import content_to_markdown
from app.widgets.card_render_core import _render_markdown_to_html_cached
from app.widgets.render import (
    HOME_CONTENT,
    HOME_TOOL,
    RenderCtx,
    active_block_index,
    diff_renders,
    render_message,
    visible_text,
)

CTX = RenderCtx(streaming=False, compact=False)


def _old_pipeline(blocks, ctx: RenderCtx = CTX) -> str:
    """旧管线：整条消息 → 一整串 markdown → 一次 convert。"""
    return _render_markdown_to_html_cached(content_to_markdown(blocks), compact=ctx.compact, lazy_body=ctx.lazy_body)


def _new_pipeline(blocks, ctx: RenderCtx = CTX) -> str:
    """新管线：逐 block 渲染后拼回整串（仅用于等价对比）。"""
    msg = render_message(blocks, ctx)
    return "\n".join(it.html for it in (*msg.content, *msg.tool))


# ── 1. 等价性 ────────────────────────────────────────────────────────

EQUIV_CASES = {
    "纯文本两段": [
        {"type": "text", "text": "第一段正文，含 **加粗** 与 `inline code`。"},
        {"type": "text", "text": "第二段正文，含 [链接](https://example.com)。"},
    ],
    "思考加正文": [
        {"type": "reasoning", "content": "推理过程第一段。\n\n推理过程第二段。"},
        {"type": "text", "text": "结论正文。"},
    ],
    "工具加正文": [
        {"type": "text", "text": "先看工具结果。"},
        {
            "type": "tool_result",
            "name": "read",
            "arguments": {"path": "/a/b.py"},
            "result": "print(1)",
            "success": True,
            "tool_call_id": "call_1",
        },
        {"type": "text", "text": "工具之后。"},
    ],
    "单个代码块": [{"type": "text", "text": "```python\nprint('hi')\n```"}],
    "正文内嵌协议文本": [{"type": "text", "text": "正文前\n\n<think>内嵌思考</think>\n\n正文后"}],
    "表格": [{"type": "text", "text": "| a | b |\n|---|---|\n| 1 | 2 |"}],
    "标题与列表": [{"type": "text", "text": "## 小标题\n\n- 一\n- 二\n- 三"}],
}


@pytest.mark.parametrize("name", list(EQUIV_CASES), ids=list(EQUIV_CASES))
def test_visible_text_equals_old_pipeline(name):
    blocks = EQUIV_CASES[name]
    assert visible_text(_new_pipeline(blocks)) == visible_text(_old_pipeline(blocks))


def test_reasoning_block_produces_think_markup():
    """思考块直出（不经协议文本往返）必须仍是 think 折叠框。"""
    msg = render_message([{"type": "reasoning", "content": "内容"}, {"type": "text", "text": "x"}], CTX)
    assert "think-block" in msg.content[0].html
    assert msg.content[0].kind == "reasoning"


def test_tool_block_keeps_tool_call_id():
    """工具块的 data-tool-call-id 是 restore / 去重判据，不能丢。"""
    blocks = [
        {
            "type": "tool_result",
            "name": "edit",
            "arguments": {"file_path": "/a.py"},
            "result": "ok",
            "success": True,
            "tool_call_id": "tc-42",
        }
    ]
    msg = render_message(blocks, CTX)
    assert msg.content[0].html.count('data-tool-call-id="tc-42"') >= 1


# ── 2. 增量最小性 ────────────────────────────────────────────────────


def test_streaming_append_replaces_only_active_block():
    """流式追加一批文本：前缀全命中，只有活动块被替换。"""
    ctx = RenderCtx(streaming=True, compact=False)
    head = [
        {"type": "text", "text": "第一段已经写完了。"},
        {
            "type": "tool_result",
            "name": "read",
            "arguments": {"path": "/a.py"},
            "result": "x",
            "success": True,
            "tool_call_id": "c1",
        },
    ]
    old = render_message([*head, {"type": "text", "text": "活动块前半"}], ctx)
    new = render_message([*head, {"type": "text", "text": "活动块前半加新内容"}], ctx)
    ops = diff_renders(old.content, new.content)
    assert ops.base == len(old.content) - 1
    assert ops.delete == 1
    assert len(ops.items) == 1


def test_stable_message_diff_is_empty():
    """内容未变时补丁必须为空（避免无意义的 DOM 抖动）。"""
    blocks = [{"type": "text", "text": "内容"}, {"type": "text", "text": "另一段"}]
    a = render_message(blocks, CTX)
    b = render_message(blocks, CTX)
    assert diff_renders(a.content, b.content).is_empty
    assert diff_renders(a.tool, b.tool).is_empty


def test_tool_result_append_does_not_touch_prefix():
    """工具结果插入中间位置：只有插入点之后重建，之前不动。"""
    ctx = RenderCtx(streaming=True, compact=False)
    before = [{"type": "text", "text": "第一段。"}]
    after = [
        {"type": "text", "text": "第一段。"},
        {
            "type": "tool_result",
            "name": "read",
            "arguments": {},
            "result": "r",
            "success": True,
            "tool_call_id": "c9",
        },
    ]
    old = render_message(before, ctx)
    new = render_message(after, ctx)
    ops = diff_renders(old.content, new.content)
    assert ops.base == 1
    assert ops.delete == 0
    assert len(ops.items) == 1


def test_identity_includes_kind_and_home():
    """同 key 不同 kind / home 必须判为不同节点（否则样式与归属会错）。"""
    a = render_message([{"type": "text", "text": "x"}], CTX).content[0]
    from app.widgets.render.blocks import BlockRender

    b = BlockRender(kind="tool", key=a.key, html=a.html, home=a.home)
    assert a.identity != b.identity


# ── 3. 归属与活动块 ──────────────────────────────────────────────────


def test_compact_mode_moves_tool_block_to_tool_home():
    ctx = RenderCtx(streaming=False, compact=True)
    blocks = [
        {
            "type": "tool_result",
            "name": "bash",
            "arguments": {"command": "ls"},
            "result": "a",
            "success": True,
            "tool_call_id": "c1",
        }
    ]
    msg = render_message(blocks, ctx)
    assert not msg.content
    assert msg.tool and msg.tool[0].home == HOME_TOOL


def test_non_compact_keeps_tool_block_in_content():
    blocks = [
        {
            "type": "tool_result",
            "name": "bash",
            "arguments": {},
            "result": "a",
            "success": True,
            "tool_call_id": "c1",
        }
    ]
    msg = render_message(blocks, CTX)
    assert msg.content and msg.content[0].home == HOME_CONTENT
    assert not msg.tool


def test_active_block_index_points_to_last_text():
    blocks = [
        {"type": "text", "text": "a"},
        {"type": "reasoning", "content": "b"},
        {"type": "reasoning", "content": "c"},
    ]
    assert active_block_index(blocks, streaming=True) == 2
    assert active_block_index(blocks, streaming=False) is None


def test_active_block_key_changes_between_completed_and_streaming():
    """活动块 key 带 completed 维度：定型后必然触发一次替换。"""
    ctx_stream = RenderCtx(streaming=True, compact=False)
    blocks = [{"type": "text", "text": "活动"}]
    streaming = render_message(blocks, ctx_stream)
    done = render_message(blocks, CTX)
    assert streaming.content[0].key != done.content[0].key
