# -*- coding: utf-8 -*-
"""回归测试：简洁分组（inline_groups）模式下思考块的结束短预览。

背景
----
简洁分组模式（ctx.inline_groups=True，随简洁阅读开启）下，思考块与正文内联
在同一个滚动面板（.dfx-group-body[data-live]）里。思考块必须保持既有观感：

* 流式活动块：``.v2-live-text.think-compact``，浅色 + 96px 限高窗口（CSS 保证）；
* 定型块：``.think-compact`` 单行短预览（浅色背景 + 采样截断文本）。

2026-11-28 回归：``_render_reasoning_block`` 新增 ``inline_groups and completed``
分支，把完成态思考块降级成无样式的裸 ``div.dfx-group-reasoning``（纯文本全高
铺开），"最后收成短预览"的效果消失。本测试锁定完成态必须走 think-compact。
"""

from app.widgets.render.block_render import render_block
from app.widgets.render.blocks import RenderCtx

_LONG_THINK = (
    "用户想要的是把渲染回归修好。首先我需要确认新旧路径的差异，"
    "然后建立可复现的反馈循环，再定位根因并修复，最后跑回归测试验证。"
    "因此结论是：完成态思考块必须保持短预览形态，不能降级成裸 div 全高铺开，"
    "否则会破坏浅色限高流式输出最后收成短预览的整体观感。"
)


def _reasoning_block():
    return {"type": "reasoning", "content": _LONG_THINK}


def test_completed_reasoning_inline_group_uses_think_compact():
    """完成态（inline_groups + compact）思考块必须是 think-compact 短预览。"""
    ctx = RenderCtx(compact=True, inline_groups=True, streaming=False)
    item = render_block(_reasoning_block(), ctx, completed=True)
    assert "think-compact" in item.html, f"完成态思考块应走 think-compact 短预览: {item.html!r}"
    assert "dfx-group-reasoning" not in item.html, f"不应降级成无样式裸 div: {item.html!r}"


def test_completed_reasoning_inline_group_preview_is_sampled():
    """短预览应是采样截断文本，而非整段思考全文铺开。"""
    from app.widgets.render.message_render import visible_text

    ctx = RenderCtx(compact=True, inline_groups=True, streaming=False)
    item = render_block(_reasoning_block(), ctx, completed=True)
    assert visible_text(item.html) == (
        "因此结论是：完成态思考块必须保持短预览形态，不能降级成裸 div 全高铺开，"
        "否则会破坏浅色限高流式输出最后收成短预览的整体观感。"
    )


def test_completed_reasoning_non_inline_group_unchanged():
    """非分组简洁模式仍走 think-compact（守护既有路径不被误改）。"""
    ctx = RenderCtx(compact=True, inline_groups=False, streaming=False)
    item = render_block(_reasoning_block(), ctx, completed=True)
    assert "think-compact" in item.html, f"非分组简洁模式路径被破坏: {item.html!r}"


def test_streaming_reasoning_live_keeps_think_compact_class():
    """流式活动块 live_html 应带 think-compact（浅色限高由骨架 CSS 提供）。"""
    from app.widgets.card_viewers import CodeWebViewer

    html = CodeWebViewer._v2_live_html("正在思考", reasoning=True)
    assert "think-compact" in html, f"流式思考尾态缺 think-compact: {html!r}"
