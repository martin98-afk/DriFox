# -*- coding: utf-8 -*-
"""回归测试：reasoning_content 流式输出思考内容泄漏到正文（高块闪现）。

现象（用户报告）
----------------
reasoning_content 模式 + 消息卡片简洁模式（compact）下，刚开始流式对话时
思考内容以普通正文显示在消息区；第一轮思考结束后思考内容消失，回到
"工具与思考"折叠区；后续轮次正常。

根因链（三层）
--------------
A. `_extract_closed_segments` 的 think 配对守卫失效：
   判断写的是 `seg.count(" think") > seg.count(" response")`，
   但 `_build_incremental_md` 生成的实际标签是 `<think>` / `</think>`
   （`<think>` 不含子串 `" think"`，`</think>` 不含 `" response"`，
   count 恒 0 → 守卫恒不触发）。多段思考内容（含 `\n\n`）被在中间
   切碎成 `<think>段1` 与 `段2</think>` 两段。
B. 切出的 `段2</think>` 段没有 `<think>` 开头 → `_inject_think_cards`
   找不到 `<think>` → 整段当普通文本返回 → markdown 渲染成
   `<p>段2</think></p>` → 思考内容以正文泄漏，`</think>` 残留。
C. 差量渲染经 `updateContentAppend` 追加到 #content-placeholder，
   该 JS 函数不调用 `reorganizeContent()`（而全量 `updateContent`
   在简洁模式下会调用）→ 泄漏的思考块滞留正文区，直到下一次
   全量渲染才被搬移/折叠 → 视觉上"思考内容在正文闪现，然后消失
   回到工具与思考折叠里"。

修复
----
A. 配对守卫改为真实标签 `<think>` / `</think>` → 未闭合 think 段
   不被差量切碎（整块等闭合后的全量渲染）。
B. 差量调用点 `_render_stable_segment(seg, compact=...)` 传
   `_tool_compact_mode` → 简洁模式下思考块渲染成 think-compact，
   与全量渲染形态一致（9c76d04f 只加了参数没改调用点）。
C. `updateContentAppend` 追加后调用 `reorganizeContent()` →
   思考/工具块立即搬移到工具区，不滞留正文。
D. 防御：`_inject_think_cards` 对无 `<think>` 开头的段落清理孤立
   `</think>` 残留，避免思考内容以正文泄漏。
"""

import sys
from pathlib import Path

from app.widgets.message_card import _extract_closed_segments, _inject_think_cards, _render_stable_segment


# ── 用例 1：配对守卫必须使用真实标签，未闭合 think 段不可被切碎 ──
def test_closed_segments_keep_unclosed_think_whole():
    """多段思考内容（含空行）不能被差量切碎成 think-streaming + 正文泄漏段。"""
    md = "<think>第一段思考内容\n\n第二段思考内容</think>\n\n正文第一段"
    stable_len, segs = _extract_closed_segments(md)
    # 修复后：`<think>第一段思考内容`（未闭合）必须触发 break，
    # 不能产出任何被切碎的段（正文段也延迟到全量渲染，由增量纯文本兜底）。
    assert segs == [], f"未闭合 think 段被切碎产出: {segs!r}"


def test_closed_segments_single_para_think_passes():
    """单段思考（无空行）已闭合 → 允许整段差量切出（与正文同段不碎）。"""
    md = "<think>简短思考</think>\n\n正文第一段"
    stable_len, segs = _extract_closed_segments(md)
    # 思考段已闭合可产出；正文段无 \n\n 结尾（未闭合）不产出
    assert len(segs) == 1, f"单段思考应产出 1 段: {segs!r}"
    assert segs[0] == "<think>简短思考</think>"


# ── 用例 2：差量渲染不得把孤立 </think> 段渲染成正文 ──
def test_render_stable_segment_no_orphan_close_tag_leak():
    """只有 </think> 的段（历史上被切碎的产物）不得残留 </think> 标签文本。

    孤立 </think> 段无 `<think>` 开头，无法恢复为 think-compact（内容不完整）；
    防御目标是清理 `</think>` 残留，避免 `<p>内容</think></p>` 的标签文本泄漏。
    切碎本身由 test_closed_segments_keep_unclosed_think_whole（配对守卫修复）杜绝。
    """
    html = _render_stable_segment("第二段思考内容</think>", compact=True)
    # 不得残留未处理的 </think> 文本标签
    assert "</think>" not in html, f"残留 </think> 泄漏: {html}"


def test_inject_think_cards_cleans_orphan_close_tag():
    """防御：无 <think> 开头的段，孤立 </think> 必须被清理而非原文透传。"""
    out = _inject_think_cards("第二段思考内容</think>", True, compact=True)
    assert "</think>" not in out


# ── 用例 3：compact 形态对齐（9c76d04f 意图）──
def test_render_stable_segment_compact_alignment():
    """已闭合 think 段：compact=True 渲染 think-compact，False 渲染 think-block。"""
    seg = "<think>完整思考内容</think>"
    compact_html = _render_stable_segment(seg, compact=True)
    block_html = _render_stable_segment(seg, compact=False)
    assert "think-compact" in compact_html, f"compact=True 应渲染 think-compact: {compact_html}"
    assert "think-block" in block_html, f"compact=False 应渲染 think-block: {block_html}"
    # 思考内容不得以普通 <p> 正文出现
    assert "<p>完整思考内容</p>" not in compact_html
    assert "<p>完整思考内容</p>" not in block_html
