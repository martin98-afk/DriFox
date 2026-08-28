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

残漏场景（本文件用例 4/5 锁定的"首轮泄漏"）
--------------------------------------------
首次流式迭代的 `append_reasoning` 首 chunk 触发全量渲染（"深度思考中"
spinner），`_apply_render_result` 把差量基线 `_stable_md_len` 推进到
**当时**的 markdown 长度——位于 `<think>` 块内部。后续 reasoning chunks
静默累积，正文到达时差量渲染从基线（think 内部）扫描，切片以
`内容</think>` 开头（无 `<think>` 配对）→ 配对守卫只比较开标签多寡，
识别不了"无开标签的残段" → 残段被当普通正文渲染 → 思考内容泄漏。

修复：
- `_apply_render_result` 基线推进守卫：`_has_unclosed_think_or_tool(md)`
  为 True（渲染对象含未闭合 `<think>`/`<tool>`）时不推进基线 → 下一次
  差量从 think 开头扫描，配对守卫正确 break，等完整闭合后整体渲染。
- `_extract_closed_segments` 起点防护：切片第一个闭合标签出现在开标签
  之前（起点位于块内部）→ 整个切片不产出，交给全量渲染兜底。
"""

import sys
from pathlib import Path

from app.widgets.message_card import (
    CodeWebViewer,
    MessageCard,
    _extract_closed_segments,
    _has_unclosed_think_or_tool,
    _inject_think_cards,
    _render_stable_segment,
    _render_think_block,
    _render_think_block_lightweight,
)


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


# ── 用例 4：差量扫描起点位于 think 块内部（首次流式基线推进）──
def test_closed_segments_rejects_slice_starting_inside_think():
    """扫描起点位于 think 块内部：切片以 `</think>` 开头且无 `<think>` 配对，
    不得产出任何残段（否则思考内容被当普通正文渲染泄漏）。

    模拟：首次流式首 chunk 全量渲染（spinner）后差量基线被推进到
    think 块内部（_stable_md_len 指向首个 reasoning chunk 末尾），
    后续内容到达时差量扫描的切片是 `内容</think>\n\n正文`。
    配对守卫（count("<think>") > count("</think>")）只比较开标签多寡，
    识别不了"无开标签的残段"——修复前残段被当普通正文差量渲染致泄漏。
    """
    md = "第二段思考内容</think>\n\n正文第一段"
    stable_len, segs = _extract_closed_segments(md)
    assert segs == [], f"think 残段被切碎产出: {segs!r}"


def test_closed_segments_rejects_multi_para_slice_inside_think():
    """多段残片（含 \\n\\n）同样必须拒绝（首段即泄漏源，不能逐段放行）。"""
    md = "第一段残行\n\n第二段残行</think>\n\n正文"
    stable_len, segs = _extract_closed_segments(md)
    assert segs == []


def test_has_unclosed_think_or_tool_detects_open_blocks():
    """基线推进守卫：md 含未闭合 `<think>`/`<tool>` 块 → True（不得推进基线）。"""
    assert _has_unclosed_think_or_tool("") is False
    assert _has_unclosed_think_or_tool("普通正文文本") is False
    assert _has_unclosed_think_or_tool("<think>第一段思考") is True
    assert _has_unclosed_think_or_tool("<think>思考A</think>\n\n<think>思考B") is True
    assert _has_unclosed_think_or_tool("<think>完整思考</think>") is False
    assert _has_unclosed_think_or_tool("<tool>name: read_file\nargs: {}") is True
    assert _has_unclosed_think_or_tool("<tool>x</tool>") is False


# ── 用例 5：思考内容本体不得进入正文 <p>（补强：不止断标签残留）──
class _StubViewer:
    """无头 viewer 桩：吞掉 JS/渲染调用，支持首轮数据链路（start_new_thinking_block
    → append_reasoning → append_text → _build_incremental_md）所需最小接口。"""

    _has_reached_clean_boundary = staticmethod(CodeWebViewer._has_reached_clean_boundary)

    def __init__(self):
        self._streaming = True
        self._reasoning_streaming_started = False
        self._think_text_streaming_started = False
        self._thinking_finalized = True
        self._lazy_markdown_cb = None
        self._tool_md_cache = {}
        self.js_calls = []
        self.render_calls = 0

    def _schedule_render(self, immediate=False):
        self.render_calls += 1

    def _append_text_incremental(self, text):
        self.js_calls.append(("append_text_incremental", text))

    def page(self):
        return self

    def runJavaScript(self, js_code):
        self.js_calls.append(js_code)


def _make_streaming_card():
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication(sys.argv)
    card = MessageCard(role="assistant")
    card._lazy_rendered = True
    card.viewer = _StubViewer()
    return card


def test_first_round_pipeline_think_content_not_in_body():
    """首轮数据链路：start_new_thinking_block → append_reasoning → append_text。

    简洁模式（compact）下，全量渲染路径（_inject_think_cards completed=False
    流式态 / 差量路径 _render_stable_segment）都不允许思考内容以
    `<p>思考内容</p>` 正文形式出现（旧 bug：首轮思考内容泄漏到正文，
    后续全量渲染才折叠消失）。
    """
    card = _make_streaming_card()
    card.start_new_thinking_block()
    card.append_reasoning("首轮思考内容")
    card.append_text("正文第一段")

    md = card._build_incremental_md()
    assert "<think>首轮思考内容</think>" in md, f"reasoning 块应转为 think 标签: {md!r}"

    # 全量渲染路径（流式态 completed=False + 简洁模式）：
    # 思考只出现在 think-* 容器，不得进入正文 <p>
    processed = _inject_think_cards(md, False, compact=True)
    assert "首轮思考内容" in processed  # 内容仍展示（think-compact 预览）
    assert "<p>首轮思考内容</p>" not in processed, f"思考内容泄漏进正文 <p>: {processed}"

    # 差量渲染路径：闭合 think 段渲染 think-compact，正文段是普通 <p>（无思考内容）
    seg_html = _render_stable_segment("<think>首轮思考内容</think>", compact=True)
    assert "think-compact" in seg_html
    assert "首轮思考内容" in seg_html
    assert "<p>首轮思考内容</p>" not in seg_html, f"差量渲染思考内容泄漏进正文 <p>: {seg_html}"


def test_second_round_thinking_stays_independent():
    """两轮迭代：第二轮思考独立成块，_build_incremental_md 不合并、不重复思考段。"""
    card = _make_streaming_card()
    # 第一轮：思考 + 正文
    card.start_new_thinking_block()
    card.append_reasoning("第一轮思考")
    card.append_text("第一轮正文")
    # 第二轮：新思考块（工具迭代后）
    card.start_new_thinking_block()
    card.append_reasoning("第二轮思考")

    md = card._build_incremental_md()
    assert md.count("<think>") == 2, f"两轮思考应各成一块: {md!r}"
    # 两轮思考各成独立块：第二轮内容不得合并进第一轮（Bug B 的块引用锚点）
    assert "<think>第二轮思考</think>" in md, f"第二轮思考应独立成块: {md!r}"

    # 全量渲染（简洁模式）：两轮思考都不得以正文 <p> 泄漏
    processed = _inject_think_cards(md, False, compact=True)
    assert "<p>第一轮思考</p>" not in processed
    assert "<p>第二轮思考</p>" not in processed
    assert processed.count("think-compact") == 2  # 两轮均已闭合 → think-compact


# ── 用例 6：流式态 spinner 必须可渲染（回归：bd44a773 引入未定义变量）──
def test_render_think_block_streaming_spinner_renders():
    """流式态（completed=False）必须产出"深度思考中" spinner，不得抛异常。

    回归背景：bd44a773 把 spinner 的内联字体样式改为引用 `font_style_inline`，
    但该变量在函数作用域内未定义 → NameError → 被 _render_markdown_to_html
    的 `except Exception` 吞掉后回退 `<pre>` 原文 → "深度思考中" spinner
    在流式期间从不出现。
    """
    for render in (_render_think_block, _render_think_block_lightweight):
        html = render("思考内容", completed=False)
        assert "think-streaming" in html, f"{render.__name__} 未产出流式 spinner: {html!r}"
        assert "深度思考中" in html, f"{render.__name__} 未产出 spinner 文案: {html!r}"
