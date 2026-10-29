# -*- coding: utf-8 -*-
"""消息级渲染：block 列表 → BlockRender 列表 + 容器划分 + 增量补丁。

与既有管线的输出对照：

| 既有 | 本模块 |
|---|---|
| ``content_to_markdown(blocks)`` 拼整串 | 不拼串，逐 block 渲染 |
| 8 道预处理扫整串 | 每道只扫单个 text block |
| ``md.convert`` 一次出整页 HTML | 每 block 一次 convert |
| ``updateContent`` 整页 innerHTML 替换 | ``patchBlocks`` 定点 splice |
| ``reorganizeContent`` 事后搬移节点 | 渲染时按 ``home`` 直接分容器 |

``render_message`` 是纯函数：同样的 blocks + ctx 任何线程都得到同样结果，
因此可以整体挪进线程池，也可以在测试里逐字比对产物。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

from app.widgets.render.block_render import CHAR_COUNT_HTML, render_blocks
from app.widgets.render.blocks import (
    HOME_CONTENT,
    HOME_TOOL,
    BlockRender,
    PatchOps,
    RenderCtx,
    diff_renders,
)


@dataclass(frozen=True)
class MessageRender:
    """一条消息的完整渲染结果，按归属容器分组。"""

    content: tuple[BlockRender, ...]
    tool: tuple[BlockRender, ...]

    @property
    def total_blocks(self) -> int:
        return len(self.content) + len(self.tool)


def active_block_index(blocks: Sequence[Dict[str, Any]], streaming: bool) -> int | None:
    """流式期间仍在增长的活动块下标（最后一个 text / reasoning 块）。

    只认 text / reasoning：工具结果一旦到达即定型（结构由 tool_call_id 决定，
    内容不再追加）。工具的「运行中 → 完成」是**另一个 block**（tool_streaming
    由 JS 注入，不在 block 列表里），因此不在此列。
    """
    if not streaming:
        return None
    for idx in range(len(blocks) - 1, -1, -1):
        blk = blocks[idx]
        if isinstance(blk, dict) and blk.get("type") in ("text", "reasoning"):
            return idx
    return None


def render_message(
    blocks: Sequence[Dict[str, Any]],
    ctx: RenderCtx,
    *,
    active_index: int | None = None,
    char_count: bool = False,
    cache=None,
    live_index: int | None = None,
    live_html: str = "",
) -> MessageRender:
    """渲染一条消息。

    Args:
        blocks: 标准内容块列表。
        ctx: 渲染上下文。
        active_index: 仍在增长的块下标；None 时按 ``ctx.streaming`` 自动推断。
        char_count: 是否在正文末尾附字符统计节点（流式期间用）。
        cache: 可选的 ``BlockCache``（按 viewer 独享）。
        live_index: 流式纯文本尾态的活动块下标（见 ``render_blocks``）。
        live_html: 活动块首帧纯文本 HTML。
    """
    if live_index is None and active_index is None and ctx.streaming:
        active_index = active_block_index(blocks, True)

    rendered = render_blocks(blocks, ctx, active_index, cache, live_index=live_index, live_html=live_html)
    content = [it for it in rendered if it.home == HOME_CONTENT]
    tool = [it for it in rendered if it.home == HOME_TOOL]

    if char_count and content:
        last = content[-1]
        # key 必须带上统计节点维度：diff 按 identity 判「节点是否要重建」，
        # 若沿用原 key，流式→非流式切换时统计节点不会被移除（html 变了但
        # identity 没变 → patch 跳过）。加 ":cc" 后缀使两种形态互异。
        content[-1] = BlockRender(
            kind=last.kind,
            key=last.key + ":cc",
            html=last.html + CHAR_COUNT_HTML,
            home=last.home,
        )
    return MessageRender(content=tuple(content), tool=tuple(tool))


def diff_message(old: MessageRender | None, new: MessageRender) -> tuple[PatchOps, PatchOps]:
    """返回 (正文容器补丁, 工具容器补丁)。"""
    if old is None:
        empty: tuple[BlockRender, ...] = ()
        return diff_renders(empty, new.content), diff_renders(empty, new.tool)
    return diff_renders(old.content, new.content), diff_renders(old.tool, new.tool)


def visible_text(html: str) -> str:
    """粗略取 HTML 可见文本（测试对比用，不追求完整 HTML 语义）。

    目的只是让「新旧管线产物是否等价」有个可断言的口径：去掉标签、压缩空白后
    比较纯文本。样式属性差异、属性顺序差异不影响判定。
    """
    import re

    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    from html import unescape

    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def collect_texts(renders: Sequence[BlockRender]) -> List[str]:
    return [visible_text(it.html) for it in renders if it.html.strip()]
