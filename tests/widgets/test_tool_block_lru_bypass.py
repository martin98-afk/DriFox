# -*- coding: utf-8 -*-
"""_render_tool_block_content 流式 bypass + lru 治理（#21）行为测试。

契约：流式（``_inject_tool_blocks`` completed=False / ``_render_tool_block``
ctx.streaming）调用直算不进 lru；历史/非流式调用走缓存且重复渲染命中；
cache_clear 生效。不需要 QApplication：纯字符串计算。
"""

from __future__ import annotations

from app.widgets.card_render_core import _inject_tool_blocks, _render_tool_block_content


def _tool_md(n: int = 4) -> str:
    parts = []
    for i in range(n):
        parts.append(
            "<tool>\nname: read_file\ntool_call_id: call_%d\nargs: {\"path\": \"a.py\"}\n"
            "result: some output %d\nsuccess: true\n</tool>" % (i, i)
        )
    return "".join(parts)


def test_streaming_call_bypasses_cache():
    _render_tool_block_content.cache_clear()
    before = _render_tool_block_content.cache_info().misses
    html = _inject_tool_blocks(_tool_md(), completed=False)
    assert html  # 渲染真的发生了
    assert _render_tool_block_content.cache_info().misses == before  # 不进缓存


def test_history_call_caches_and_hits():
    _render_tool_block_content.cache_clear()
    md = _tool_md()
    _inject_tool_blocks(md, completed=True)
    info1 = _render_tool_block_content.cache_info()
    assert info1.misses == 4  # 历史路径进缓存
    _inject_tool_blocks(md, completed=True)
    info2 = _render_tool_block_content.cache_info()
    assert info2.hits == info1.hits + 4  # 重复渲染（历史重绘/主题切换）命中


def test_cache_clear_works():
    _inject_tool_blocks(_tool_md(), completed=True)
    assert _render_tool_block_content.cache_info().currsize > 0
    _render_tool_block_content.cache_clear()
    info = _render_tool_block_content.cache_info()
    assert info.currsize == 0 and info.hits == 0 and info.misses == 0


def test_block_render_streaming_bypasses_cache():
    """block_render 工具块路径：ctx.streaming=True 直算，False 走缓存。"""
    from app.widgets.render.block_render import _render_tool_block
    from app.widgets.render.blocks import RenderCtx

    block = {
        "type": "tool_result",
        "tool_call_id": "call_x",
        "name": "read_file",
        "arguments": {"path": "a.py"},
        "result": "output text",
        "success": True,
    }
    _render_tool_block_content.cache_clear()
    before = _render_tool_block_content.cache_info().misses
    html = _render_tool_block(block, RenderCtx(streaming=True))
    assert html
    assert _render_tool_block_content.cache_info().misses == before
    _render_tool_block(block, RenderCtx(streaming=False))
    assert _render_tool_block_content.cache_info().misses == before + 1
