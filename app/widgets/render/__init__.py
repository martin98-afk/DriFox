# -*- coding: utf-8 -*-
"""block 级渲染管线（步骤 1：纯函数层）。

与 ``card_render_core`` / ``card_viewers`` 的分工：

- ``card_render_core``：既有的整串 markdown 渲染实现，仍是被单 block 渲染器
  调用的底层工具函数（预处理、注入、代码块包装）。
- 本包：新增的 block 级纯函数层，一个 block 一个 DOM 节点，增量 = 前缀后缀 diff。

本包当前**不接任何调用点**，只有测试在跑（见 ``tests/widgets/test_block_render.py``）。
接入路径见设计文档 docs/superpowers/specs/2026-11-02-block-render-pipeline-design.md。
"""

from app.widgets.render.blocks import (
    HOME_CONTENT,
    HOME_TOOL,
    KIND_CUSTOM,
    KIND_IMAGE,
    KIND_REASONING,
    KIND_TEXT,
    KIND_TOOL,
    LIVE_KEY_PREFIX,
    BlockRender,
    PatchOps,
    RenderCtx,
    diff_renders,
    fingerprint,
    live_key,
)
from app.widgets.render.block_render import CHAR_COUNT_HTML, render_block, render_blocks
from app.widgets.render.cache import BlockCache
from app.widgets.render.message_render import (
    MessageRender,
    active_block_index,
    collect_texts,
    diff_message,
    render_message,
    visible_text,
)

__all__ = [
    "CHAR_COUNT_HTML",
    "HOME_CONTENT",
    "HOME_TOOL",
    "KIND_CUSTOM",
    "KIND_IMAGE",
    "KIND_REASONING",
    "KIND_TEXT",
    "KIND_TOOL",
    "LIVE_KEY_PREFIX",
    "BlockRender",
    "BlockCache",
    "MessageRender",
    "PatchOps",
    "RenderCtx",
    "active_block_index",
    "collect_texts",
    "diff_message",
    "diff_renders",
    "fingerprint",
    "live_key",
    "render_block",
    "render_blocks",
    "render_message",
    "visible_text",
]
