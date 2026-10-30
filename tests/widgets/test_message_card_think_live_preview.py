# -*- coding: utf-8 -*-
"""回归测试：流式思考实时预览（live 区节流推送 + 各完成时机清空缓冲）。

功能背景
--------
思考流式期间不再只显示"深度思考中" spinner：流式块内新增实时预览区
（.think-streaming-live，CSS 限高 96px、底部 mask 渐隐），append_reasoning
把增量文本缓冲进 _streaming_live_buffer，由 250ms 单发 QTimer 批量
textNode append（不重建 DOM、不触发全量渲染、不报外部高度）。

关键约束（由历史 bug 决定）
--------------------------
1. 预览更新不得触发全量渲染 / _schedule_render（旧坑：think-streaming
   DOM 反复 destroy+recreate 导致"思考中"状态闪烁）。
2. 预览更新不得报外部高度（旧坑：思考区内部高度变化强制外部聊天列表
   滚底，打断正文阅读）→ live 区 CSS 限高。
3. 思考块完成（工具参数到达）/ 新轮思考开始（start_new_thinking_block）
   时必须清空缓冲：残余文本串进新块的 live 区开头 = 串块。
4. flush 的 JS 用 createTextNode 安全文本节点（无 HTML 注入面），
   选择器只命中 data-streaming="true" 的活动块（同一时刻只有一个）。
"""

import sys

from app.widgets.message_card import (
    MessageCard,
    _render_think_block,
    _render_think_block_lightweight,
)


class _StubViewer:
    """无头 viewer 桩：记录 runJavaScript / 渲染调用，不做任何 DOM 操作。"""

    def __init__(self):
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
        html = render("思考内容", completed=False)
        assert 'class="think-streaming-live"' in html, f"{render.__name__} 缺 live 区容器: {html!r}"
        assert 'class="think-streaming-live-text"' in html, f"{render.__name__} 缺 live 文本节点容器: {html!r}"
        # 2026-10-31：spinner 头部行移除（直接显示流式文字），占位文案移至 CSS :empty::before
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

    assert card._streaming_live_buffer == "第一段第二段", f"缓冲累积错误: {card._streaming_live_buffer!r}"
    assert card._streaming_live_timer.isActive(), "节流 timer 未启动"
    # 关键约束 1：预览更新不得触发全量渲染（仅首 chunk 的 1 次 spinner 渲染）
    assert card.viewer.render_calls == 1, f"后续 chunk 触发了额外渲染: {card.viewer.render_calls}"
    # 关键约束：250ms 内不发 live 预览 JS（节流，非逐 chunk runJavaScript）。
    # start_new_thinking_block 自身的旧块标记 JS 不算。
    live_js = [j for j in card.viewer.js_calls if "think-streaming-live-text" in j]
    assert live_js == [], f"append 阶段不应有 live 预览 JS: {live_js!r}"


# ── flush：textNode append JS 串结构正确，缓冲清空 ──
def test_flush_pushes_textnode_js():
    card = _make_streaming_card()
    card.start_new_thinking_block()
    card.append_reasoning("思考片段α")
    card._flush_streaming_live()

    assert card._streaming_live_buffer == "", "flush 后缓冲未清空"
    flush_js = [j for j in card.viewer.js_calls if "think-streaming-live-text" in j]
    assert len(flush_js) == 1, f"应恰好一次 live 预览 JS: {len(flush_js)}"
    js = flush_js[0]
    # 安全文本节点（无 innerHTML 注入面）
    assert "createTextNode" in js, f"未用 createTextNode: {js!r}"
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
    card.append_reasoning("将被完成的思考")
    card._maybe_finish_thinking_for_tool("tool-1")

    assert card._streaming_live_buffer == "", "块完成后缓冲未清空"
    assert not card._streaming_live_timer.isActive(), "块完成后 timer 未停"
    # 关键约束 3：清空而非 flush——DOM 上的 live 区即将被折叠框替换
    live_append_js = [j for j in card.viewer.js_calls if "think-streaming-live-text" in j]
    assert live_append_js == [], f"块完成时不应向 live 区发 append JS: {live_append_js!r}"


# ── 新轮思考开始：旧块残余不得串进新块 live 区 ──
def test_start_new_block_discards_buffer():
    card = _make_streaming_card()
    card.start_new_thinking_block()
    card.append_reasoning("旧块残余")
    card.start_new_thinking_block()

    assert card._streaming_live_buffer == "", "新轮开始后旧残余未清空"
    assert not card._streaming_live_timer.isActive(), "新轮开始后 timer 未停"

    # 新块继续接收：缓冲只含新块文本
    card.append_reasoning("新块文本")
    assert card._streaming_live_buffer == "新块文本", f"新块缓冲被旧残余污染: {card._streaming_live_buffer!r}"


# ── flush 幂等：timer 触发后再次手动 flush 为 no-op ──
def test_flush_idempotent_after_drain():
    card = _make_streaming_card()
    card.start_new_thinking_block()
    card.append_reasoning("一次即可")
    card._flush_streaming_live()
    card._flush_streaming_live()
    flush_js = [j for j in card.viewer.js_calls if "think-streaming-live-text" in j]
    assert len(flush_js) == 1, f"重复 flush 应为 no-op: {len(flush_js)} 次"


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
    assert card._streaming_live_buffer == "", "defer_render 污染了 live 缓冲"
    assert not card._streaming_live_timer.isActive(), "defer_render 启动了节流 timer"
