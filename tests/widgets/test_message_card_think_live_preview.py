# -*- coding: utf-8 -*-
"""流式思考首帧、节流续写、切块与历史预览的回归测试。"""

import sys

from app.widgets.message_card import (
    MessageCard,
    _render_think_block,
    _render_think_block_lightweight,
)


class _StubViewer:
    """无头 viewer 桩：记录 runJavaScript / 渲染调用，不做任何 DOM 操作。"""

    def __init__(self):
        self._render_v2 = False
        self._streaming = True
        self._reasoning_streaming_started = False
        self._think_text_streaming_started = False
        self._thinking_finalized = True
        self._lazy_markdown_cb = None
        self._tool_md_cache = {}
        self._tool_compact_mode = False
        self.js_calls = []
        self.render_calls = 0

    def _schedule_render(self, immediate=False):
        self.render_calls += 1

    def page(self):
        return self

    def runJavaScript(self, js_code):
        self.js_calls.append(js_code)


def _make_streaming_card():
    from PyQt5.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication(sys.argv)
    card = MessageCard(role="assistant")
    card._lazy_rendered = True
    card.viewer = _StubViewer()
    return card


# ── 流式态模板必须包含 live 预览区结构 ──
def test_streaming_template_contains_live_area():
    for render in (_render_think_block, _render_think_block_lightweight):
        html = render("先检查 <b> & 继续", completed=False)
        assert "先检查 &lt;b&gt; &amp; 继续" in html
        assert 'class="think-streaming-live"' in html, f"{render.__name__} 缺 live 区容器: {html!r}"
        assert 'class="think-streaming-live-text"' in html, f"{render.__name__} 缺 live 文本节点容器: {html!r}"
        assert "tool-streaming-spinner" not in html, f"{render.__name__} 仍残留 spinner: {html!r}"


# ── 完成态折叠框模板不受影响（回归守护：仍可渲染）──
def test_completed_template_unchanged_shape():
    html = _render_think_block("思考内容", completed=True)
    assert "think-block" in html
    assert "think-streaming-live" not in html, "完成态不应包含 live 预览区"


# ── append_reasoning 缓冲 + 节流：不触发渲染、不直接发 JS ──
def test_append_reasoning_buffers_and_throttles():
    card = _make_streaming_card()
    card.start_new_thinking_block()
    card.append_reasoning("第一段")
    card.append_reasoning("第二段")

    assert card.viewer._lazy_markdown_cb() == "<think>第一段第二段</think>"
    assert card._streaming_live_timer.isActive(), "节流 timer 未启动"
    # 关键约束 1：预览更新不得触发全量渲染（仅首 chunk 的 1 次预览渲染）
    assert card.viewer.render_calls == 1, f"后续 chunk 触发了额外渲染: {card.viewer.render_calls}"
    # 关键约束：250ms 内不发 live 预览 JS（节流，非逐 chunk runJavaScript）。
    # start_new_thinking_block 自身的旧块标记 JS 不算。
    live_js = [j for j in card.viewer.js_calls if "think-streaming-live-text" in j]
    assert live_js == [], f"append 阶段不应有 live 预览 JS: {live_js!r}"


# 旧 Web 渲染器从最新快照刷新，延迟首帧也不会重复追加。
def test_legacy_flush_updates_text_content():
    card = _make_streaming_card()
    card.start_new_thinking_block()
    card.append_reasoning("首段")
    card.append_reasoning("思考片段α")
    card._flush_streaming_live()

    flush_js = [j for j in card.viewer.js_calls if "think-streaming-live-text" in j]
    assert len(flush_js) == 1, f"应恰好一次 live 预览 JS: {len(flush_js)}"
    js = flush_js[0]
    # 安全文本节点（无 innerHTML 注入面）
    assert 't.textContent = "首段思考片段α"' in js
    assert "innerHTML" not in js, f"flush JS 不得使用 innerHTML: {js!r}"
    # 只命中活动流式块
    assert '.think-streaming[data-streaming="true"] .think-streaming-live-text' in js, f"选择器错误: {js!r}"
    # 内部滚底 + 文本内容在场
    assert "scrollTop" in js, f"缺内部滚底: {js!r}"
    assert "思考片段α" in js, f"缓冲文本未进 JS: {js!r}"


# ── 空缓冲 flush 是 no-op ──
def test_flush_empty_buffer_noop():
    card = _make_streaming_card()
    card._flush_streaming_live()
    assert card.viewer.js_calls == []


# ── 思考块完成（工具参数到达）：清空缓冲，不向将死的 DOM 发 append ──
def test_finish_thinking_discards_buffer():
    card = _make_streaming_card()
    card.start_new_thinking_block()
    card.append_reasoning("将被完成")
    card.append_reasoning("的思考")
    assert card._streaming_live_timer.isActive()
    card._maybe_finish_thinking_for_tool("tool-1")

    assert not card._streaming_live_timer.isActive(), "块完成后 timer 未停"
    # 关键约束 3：清空而非 flush——DOM 上的 live 区即将被折叠框替换
    live_append_js = [j for j in card.viewer.js_calls if "think-streaming-live-text" in j]
    assert live_append_js == [], f"块完成时不应向 live 区发 append JS: {live_append_js!r}"


# ── 新轮思考开始：旧块残余不得串进新块 live 区 ──
def test_start_new_block_discards_buffer():
    card = _make_streaming_card()
    card.start_new_thinking_block()
    card.append_reasoning("旧块")
    card.append_reasoning("旧块残余")
    assert card._streaming_live_timer.isActive()
    card.start_new_thinking_block()

    assert not card._streaming_live_timer.isActive(), "新轮开始后 timer 未停"

    # 新块继续接收时只显示新块文本。
    card.append_reasoning("新块文本")
    card._flush_streaming_live()
    assert 't.textContent = "新块文本"' in card.viewer.js_calls[-1]


def test_inline_think_continues_after_tag_only_first_chunk():
    card = _make_streaming_card()
    card.viewer._render_v2 = True
    card.append_text("<think>")
    assert not card.viewer._think_text_streaming_started
    card.append_text("第一行")
    assert card.viewer._think_text_streaming_started
    assert not card._streaming_live_timer.isActive()
    card.append_text("第二行")
    assert card._streaming_live_timer.isActive()
    assert card.viewer.render_calls == 2
    card._flush_streaming_live()
    assert card.viewer.render_calls == 3
    assert card.viewer._lazy_markdown_cb() == "<think>第一行第二行"


def test_native_think_card_updates_live_plain_text(qapp):
    from PyQt5.QtCore import Qt
    from app.widgets.markdown_block_viewer import ThinkCard

    card = ThinkCard("<b>首段</b>", completed=False)
    assert card._title_label.isHidden()
    assert card._preview_label.textFormat() == Qt.PlainText
    assert card._preview_label.text() == "<b>首段</b>"
    card.update_content({"content": "首段\n第二段", "completed": False})
    assert card._preview_label.text() == "首段 第二段"
    card.apply_streaming(False)
    assert not card._title_label.isHidden()


# ── 历史批量加载：defer_render + force_new_block（2026-10-31 思考沉底修复）──
def test_history_batch_reasoning_independent_blocks():
    """历史加载路径：每条 assistant 消息的思考必须独立成块、不触发渲染。

    根因回归：render_batch_to_assistant_card 旧实现把 reasoning 拼进 text 协议串
    （<think>…</think>\\n\\n正文），思考卡混在 text 块 wrapper 内，v2 简洁模式靠
    _v2AfterPatch 搬到工具区且插入点恒在最后一个 wrapper 之后 → 所有思考沉底。
    修复后走独立 reasoning 块（patchBlocks 保序）。本测试锁数据层行为：
    force_new_block 逐条独立成块；defer_render 不触发任何渲染/JS/buffer。
    """
    card = _make_streaming_card()
    card.append_reasoning("第一条消息的思考", defer_render=True, force_new_block=True)
    card.append_reasoning("第二条消息的思考", defer_render=True, force_new_block=True)

    blocks = [b for b in card._content_data if b.get("type") == "reasoning"]
    assert len(blocks) == 2, f"两条思考应独立成块: {card._content_data!r}"
    assert blocks[0]["content"] == "第一条消息的思考"
    assert blocks[1]["content"] == "第二条消息的思考"
    # defer_render：零渲染、零 JS、零 live 缓冲（渲染由 finish_streaming(history=True) 统一）
    assert card.viewer.render_calls == 0, f"defer_render 触发了渲染: {card.viewer.render_calls}"
    assert card.viewer.js_calls == [], f"defer_render 触发了 JS: {card.viewer.js_calls!r}"
    assert not card._streaming_live_timer.isActive(), "defer_render 启动了节流 timer"
