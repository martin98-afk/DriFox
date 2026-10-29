# -*- coding: utf-8 -*-
"""单 block → HTML 渲染器（纯函数，线程安全）。

对既有管线的切分方式：既有实现把「整条消息的 markdown 串」一次性跑完 8 道
预处理再 ``md.convert``；本模块改为**逐个 block** 跑同一批函数，产物按 block
切成 ``BlockRender`` 列表。所有预处理函数直接复用 ``card_render_core`` 里的
实现（纯字符串变换，无副作用），不重写语义。

线程安全：``Markdown`` 实例与 ``HtmlFormatter`` 不可跨线程共享
（``reset()`` 与 formatter 内部状态都不是线程安全的），故用线程局部实例，
与 ``card_render_core._render_markdown_to_html_worker`` 同款做法。

已知偏差（步骤 1 显式接受，步骤 4 处理）：
- 工具块仍走 ``content_to_markdown([block])`` → 协议文本 → ``_render_tool_block_content``
  的往返，保持与既有产物逐字等价；该往返会把结果截断到 300 字符
  （``message_content.py`` 的 ``result_escaped[:300]``），是存量缺陷，不在本步修。
- ``<think>`` 的 ``data-flip-key`` 序号按 block 局部计数，渲染后统一平移到
  消息级序号（见 ``_renumber_flip_keys``），保证与整串渲染一致。
"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Sequence

from app.widgets.card_render_core import (
    _CHAR_COUNT_HTML,
    _extract_fenced_code,
    _extract_formulas,
    _inject_context_links,
    _inject_hook_blocks,
    _inject_tag_cards,
    _inject_think_cards,
    _inject_tool_blocks,
    _protect_inline_svg_blocks,
    _render_think_block,
    _render_tool_block_content,
    _resolve_image_src,
    _restore_fenced_code,
    _sanitize_incomplete_markdown,
    _unwrap_code_blocks_with_context_links,
    _wrap_code_blocks_with_copy_button_web,
)
from app.widgets.render.blocks import (
    HOME_CONTENT,
    HOME_TOOL,
    KIND_CUSTOM,
    KIND_IMAGE,
    KIND_REASONING,
    KIND_TEXT,
    KIND_TOOL,
    BlockRender,
    RenderCtx,
    fingerprint,
)

# 消息级字符统计节点（流式尾部显示），不属于任何 block，由 message_render 单独拼接
CHAR_COUNT_HTML = _CHAR_COUNT_HTML

_tls = threading.local()

_FLIP_KEY_RE = re.compile(r'data-flip-key="think-(\d+)"')


def _thread_markdown():
    """线程局部 Markdown 实例（配置与全局实例一致）。"""
    md = getattr(_tls, "md", None)
    if md is None:
        from markdown import Markdown

        md = Markdown(extensions=["fenced_code", "nl2br", "tables"], output_format="html5", safe=False)
        _tls.md = md
    return md


def _thread_formatter(style: str, font_size: int):
    """线程局部 Pygments formatter（style / 字号变化时重建）。"""
    key = (style, font_size)
    if getattr(_tls, "fmt_key", None) != key:
        from pygments.formatters import HtmlFormatter

        pre_color = "#D4D4D4" if style == "dracula" else "#1a1a1a"
        _tls.fmt = HtmlFormatter(
            style=style,
            linenos=False,
            noclasses=True,
            cssclass="code-block",
            prestyles=(
                "margin:0; padding:0; background:transparent; "
                f"font-family: Consolas, monospace; font-size:{font_size}px; color:{pre_color};"
            ),
        )
        _tls.fmt_key = key
    return _tls.fmt


def _convert(md_text: str, ctx: RenderCtx) -> str:
    """markdown 串 → HTML（含代码块包装与图片路径解析）。"""
    md = _thread_markdown()
    md.reset()
    html = md.convert(md_text)
    html = _wrap_code_blocks_with_copy_button_web(
        html,
        icon_prefix=ctx.icon_prefix or None,
        font_size=ctx.code_font_size,
        formatter=_thread_formatter(ctx.pygments_style, ctx.code_font_size),
    )
    return _resolve_image_src(html)


def _prepare_text(md_text: str, ctx: RenderCtx, completed: bool) -> str:
    """8 道预处理（作用域限定在单个 text block 内）。

    顺序与 ``_render_markdown_to_html_cached_impl`` 严格一致：sanitize →
    inline svg 保护 → 公式提取 → 代码块解包 → 上下文链接 → fence 哨兵 →
    think / tool / hook / tag 注入 → fence 还原。任何一步换位置都会改变产物。
    """
    safe_md = _sanitize_incomplete_markdown(md_text)
    safe_md = _protect_inline_svg_blocks(safe_md)
    safe_md = _extract_formulas(safe_md)
    safe_md = _unwrap_code_blocks_with_context_links(safe_md)
    safe_md = _inject_context_links(safe_md)
    fences, safe_md = _extract_fenced_code(safe_md)
    safe_md = _inject_think_cards(safe_md, completed, compact=ctx.compact)
    safe_md = _inject_tool_blocks(
        safe_md,
        completed,
        compact=ctx.compact,
        heavy_caps=ctx.heavy_caps,
        suppress=ctx.tool_fingerprints or None,
        lazy_body=ctx.lazy_body,
    )
    safe_md = _inject_hook_blocks(safe_md, completed)
    safe_md = _inject_tag_cards(safe_md, completed, compact=ctx.compact)
    return _restore_fenced_code(safe_md, fences)


def _renumber_flip_keys(html: str, offset: int) -> str:
    """把 block 局部的 think 序号平移到消息级序号。

    ``_inject_think_cards`` 每次都从 0 开始计数；按 block 分片渲染时若不纠正，
    不同 block 里的思考块会共用同一个 ``data-flip-key``，FLIP 动画配对会错乱。
    """
    if offset <= 0 or "data-flip-key" not in html:
        return html
    return _FLIP_KEY_RE.sub(lambda m: f'data-flip-key="think-{int(m.group(1)) + offset}"', html)


def _count_think_blocks(html: str) -> int:
    return html.count("data-flip-key=")


def _render_text_block(text: str, ctx: RenderCtx, completed: bool, flip_offset: int) -> tuple[str, int]:
    """文本块渲染，返回 (html, 本块消耗的 think 序号数)。"""
    processed = _prepare_text(text, ctx, completed)
    html = _convert(processed, ctx)
    html = _renumber_flip_keys(html, flip_offset)
    return html, _count_think_blocks(html)


def _render_reasoning_block(block: Dict[str, Any], ctx: RenderCtx, completed: bool, flip_offset: int) -> str:
    """思考块直出（不经协议文本往返）。"""
    content = str(block.get("content", "") or "")
    if not content.strip():
        return ""
    return _render_think_block(content, completed=completed, compact=ctx.compact, flip_idx=flip_offset)


def _render_tool_block(block: Dict[str, Any], ctx: RenderCtx) -> str:
    """工具块渲染。

    走 ``content_to_markdown([block])`` → 协议文本 → ``_render_tool_block_content``
    的往返，与既有 ``_build_incremental_md`` 的产物逐字一致（步骤 1 要求等价）。
    """
    from app.core.conversation.message_content import content_to_markdown

    proto = content_to_markdown([block])
    if not proto.strip():
        return ""
    return _render_tool_block_content(
        proto.strip(),
        compact=ctx.compact,
        heavy_caps=ctx.heavy_caps,
        lazy_body=ctx.lazy_body,
    )


def _render_image_block(block: Dict[str, Any], ctx: RenderCtx) -> str:
    """图片块：转成 markdown 图片引用后走文本管线（与既有行为一致）。"""
    from app.core.conversation.message_content import content_to_markdown

    md_ref = content_to_markdown([block])
    if not md_ref.strip():
        return ""
    return _convert(_prepare_text(md_ref, ctx, True), ctx)


def _render_custom_block(block: Dict[str, Any]) -> str:
    """自定义块：插件渲染器已产出 HTML，直接使用（不再经 md.convert）。"""
    from app.core.conversation.message_content import content_to_markdown

    return content_to_markdown([block])


def _tool_home(block: Dict[str, Any], ctx: RenderCtx) -> str:
    """工具块归属容器。

    简洁模式下工具/思考块进「工具与思考」折叠区；编辑类等
    ``keep_in_content`` 工具始终留在正文（与 ``_keep_in_content_tools`` 同源）。
    """
    if not ctx.compact:
        return HOME_CONTENT
    from app.widgets.render_helpers import _keep_in_content_tools

    name = str(block.get("name", "") or "")
    if name and name in _keep_in_content_tools():
        return HOME_CONTENT
    return HOME_TOOL


def _reasoning_home(ctx: RenderCtx) -> str:
    return HOME_TOOL if ctx.compact else HOME_CONTENT


def block_key(block: Dict[str, Any], ctx: RenderCtx, completed: bool) -> tuple[str, str]:
    """(kind, key) —— 只算指纹不渲染，供缓存查表用。

    与 ``render_block`` 内联计算的 key 必须逐字一致，否则缓存永不命中（漏改
    一处即静默退化为每次全渲）。两侧共用本函数，不各写一份。
    """
    btype = block.get("type")

    if btype == "reasoning":
        content = str(block.get("content", "") or "")
        return KIND_REASONING, fingerprint("reasoning", content, completed, ctx.compact)

    if btype == "tool_result":
        return KIND_TOOL, fingerprint(
            "tool",
            block.get("tool_call_id", ""),
            block.get("name", ""),
            bool(block.get("success", True)),
            ctx.compact,
        )

    if btype in ("image_url", "input_image", "image"):
        return KIND_IMAGE, fingerprint(
            "image", str(block.get("image_url", "")) or str(block.get("image", "")), ctx.compact
        )

    if btype == "custom":
        return KIND_CUSTOM, fingerprint("custom", block.get("custom_type", ""), repr(block.get("data")), ctx.compact)

    # text 与未知类型统一按文本处理（与 ensure_content_blocks 的兜底语义一致）
    text = str(block.get("text", "") or "")
    return KIND_TEXT, fingerprint("text", text, completed, ctx.compact)


def render_block(block: Dict[str, Any], ctx: RenderCtx, completed: bool, flip_offset: int = 0) -> BlockRender:
    """单个 block → BlockRender。

    Args:
        block: 标准内容块（``ensure_content_blocks`` 的产物）。
        ctx: 渲染上下文。
        completed: 本块是否已定型（流式期间最后一个活动块为 False）。
        flip_offset: 本块之前已出现的 think 块数量（消息级序号起点）。
    """
    btype = block.get("type")
    kind, key = block_key(block, ctx, completed)

    if btype == "reasoning":
        html = _render_reasoning_block(block, ctx, completed, flip_offset)
        return BlockRender(kind=kind, key=key, html=html, home=_reasoning_home(ctx))

    if btype == "tool_result":
        html = _render_tool_block(block, ctx)
        return BlockRender(kind=kind, key=key, html=html, home=_tool_home(block, ctx))

    if btype in ("image_url", "input_image", "image"):
        html = _render_image_block(block, ctx)
        return BlockRender(kind=kind, key=key, html=html, home=HOME_CONTENT)

    if btype == "custom":
        html = _render_custom_block(block)
        return BlockRender(kind=kind, key=key, html=html, home=HOME_CONTENT)

    # text 与未知类型
    text = str(block.get("text", "") or "")
    if not text:
        return BlockRender(kind=kind, key=key, html="", home=HOME_CONTENT)
    html, _ = _render_text_block(text, ctx, completed, flip_offset)
    return BlockRender(kind=kind, key=key, html=html, home=HOME_CONTENT)


def render_blocks(
    blocks: Sequence[Dict[str, Any]],
    ctx: RenderCtx,
    active_index: int | None = None,
    cache=None,
) -> List[BlockRender]:
    """把一条消息的 block 列表渲染为 BlockRender 列表。

    Args:
        blocks: 标准内容块列表。
        ctx: 渲染上下文。
        active_index: 仍在流式增长的块下标（None = 全部定型）。该块的 key 会
            带上 ``completed=False``，且文本块跳过协议标签的「已完成」判定。
        cache: 可选的 ``BlockCache``。传入后已定型块直接命中，不再重算 ——
            流式期间每批只渲染一个活动块，其余零成本。**缓存必须按 viewer 独享**
            （compact / lazy_body / 主题等 ctx 差异会让 key 相同而产物不同时，
            只有同 ctx 的缓存才是安全的，key 已含 compact）。
    """
    out: List[BlockRender] = []
    flip_offset = 0
    for idx, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        completed = not (ctx.streaming and active_index == idx)
        kind, key = block_key(block, ctx, completed)
        cached = cache.get(key) if cache is not None else None
        if cached is not None:
            item = cached
        else:
            item = render_block(block, ctx, completed, flip_offset)
            if cache is not None and completed:
                # 只缓存已定型块：活动块每批都变，缓存它只会挤掉真正稳定的条目
                cache.put(key, item)
        flip_offset += _count_think_blocks(item.html)
        out.append(item)
    return out
