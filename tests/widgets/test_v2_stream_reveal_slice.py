# -*- coding: utf-8 -*-
"""v2 流式帧级揭示 + 前缀切片 + 尾部行内格式化回归。

对应 2026-10-31 的流式观感改造（A/B/C 三项）：

A. **帧级揭示**：Python 下发节奏由批处理决定（软边界 100ms 合并 / 安全定时器
   150~500ms），直接 append 到 DOM 就是「文字一块块蹦」。JS 侧 ``window._v2tw``
   队列把到达节奏与显示节奏解耦（rAF 指数追赶，同 v1 打字机算法）。
B. **前缀切片**：``_extract_closed_segments`` 切出已闭合段立刻走 markdown，
   未闭合尾部保持纯文本 —— 修「格式露源码」。
B'. **尾部行内格式化**：无空行中文长段落切不出闭合段（实测 stable=0），
   由 ``_render_inline_tail`` 整体行内渲染（对齐 v1 的 ``_render_tail_inline``）。
C. **逐帧高度上报**：揭示期间每 40ms 报一次高度，修「容器憋一下再蹦高」。

本文件覆盖 A/B/B' 的 Python 侧决策逻辑与 JS 侧函数存在性；C 的节流在 JS 内，
由骨架编译测试与常量断言兜底。
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

import pytest

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from app.widgets.card_viewers import CodeWebViewer
from app.widgets.render import BlockCache, RenderCtx, live_key

def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)

class _StubPage:
    def __init__(self):
        self.js_calls: List[str] = []

    def runJavaScript(self, js_code, callback=None):
        self.js_calls.append(js_code)

class _FakeCard:
    def __init__(self, content_data: Any):
        self._content_data = content_data

def _make_viewer(monkeypatch, blocks: Any, streaming: bool = True):
    _ensure_qapp()
    v = CodeWebViewer.__new__(CodeWebViewer)
    v._is_js_ready = True
    v._streaming = streaming
    v._light_skeleton = False
    v._dock_sync_requested = True
    v._render_v2 = True
    v._block_cache = BlockCache()
    v._v2_last = None
    v._v2_ctx_sig = None
    v._v2_live_key = ""
    v._v2_live_sent = ""
    v._v2_live_formalized_len = 0
    v._v2_live_formalize_ts = 0.0
    v._v2_live_finalize_requested = False
    v._v2_live_tail_done_len = 0
    v._v2_live_tail_done_ts = 0.0
    v._v2_live_tail_html_pending = None
    v._v2_live_whole_done_len = 0
    v._render_timer = QTimer()
    v._last_render_ts = 0.0
    v._markdown_text = "md"
    v._lazy_markdown_cb = None
    v._needs_full_render = True
    v._final_render_pending = False
    v._height_report_pending = False
    v._has_charts = False
    v._last_rendered_markdown = ""
    v._render_deferred = False
    v._page = _StubPage()
    monkeypatch.setattr(CodeWebViewer, "page", lambda self: self._page)
    monkeypatch.setattr(CodeWebViewer, "isVisible", lambda self: True)
    monkeypatch.setattr(CodeWebViewer, "parent", lambda self: _FakeCard(blocks))
    monkeypatch.setattr(CodeWebViewer, "_sync_streaming_dock", lambda self, active, collapse_after=False: None)
    monkeypatch.setattr(CodeWebViewer, "_v2_render_ctx", lambda self: RenderCtx(streaming=streaming))
    # _tool_compact_mode 是只读 property，不能直接赋实例属性
    monkeypatch.setattr(CodeWebViewer, "_tool_compact_mode", property(lambda self: False))
    return v

def _all_js(v) -> str:
    return "".join(v._page.js_calls)

def _long_paragraph(n: int = 600) -> str:
    """无空行的中文长段落：``_extract_closed_segments`` 切不出任何闭合段。"""
    return "这是一段没有空行分隔的中文长正文内容。" * (n // 19 + 1)

def _long_with_boundary(n: int = 600) -> str:
    return "段落内容测试。" * (n // 7 + 1) + "\n\n" + "第二段内容。" * 40 + "\n\n"

# ── A. 帧级揭示：Python 只用 _v2LiveAppend 入口（JS 内部再按帧揭示）────────

def test_growth_goes_through_frame_queue_entry(monkeypatch):
    """续写必须走 _v2LiveAppend（进 JS 揭示队列），而不是直接改 DOM。"""
    text = _long_paragraph(900)
    blocks = [{"type": "text", "text": text}]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True

    v._page.js_calls.clear()
    v._needs_full_render = False
    blocks[0]["text"] = text + "续写的下一句话。"
    assert v._perform_update_v2() is True
    js = _all_js(v)
    assert "_v2LiveAppend('content-placeholder',\"live:0\"" in js

def test_force_full_drops_reveal_buffer(monkeypatch):
    """整页清空前必须 drop 揭示缓冲：清空会销毁 live 节点，缓冲无处可落。"""
    blocks = [{"type": "text", "text": _long_paragraph()}]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True
    js = _all_js(v)
    assert "_v2LiveDrop" in js, "force_full 必须先丢弃揭示缓冲"

def test_partial_patch_flushes_reveal_buffer(monkeypatch):
    """局部 patch 前必须 flush：未上屏的增量 Python 不会再下发，丢掉就真丢了。"""
    blocks = [
        {"type": "text", "text": _long_paragraph()},
        {"type": "reasoning", "content": "思考内容" * 100},
    ]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True

    v._page.js_calls.clear()
    v._needs_full_render = False
    blocks.append({"type": "tool_result", "name": "read", "tool_call_id": "t1", "result": "{}", "success": True})
    assert v._perform_update_v2() is True
    assert "_v2LiveFlush" in _all_js(v), "局部 patch 前必须 flush 揭示缓冲"

# ── B. 前缀切片：闭合段立刻走 markdown ────────────────────────────────

def test_slice_progresses_on_paragraph_boundary(monkeypatch):
    """有空行边界的文本：切片推进 formalized_len 并下发 _v2LiveSplit。"""
    base = _long_paragraph(600)
    text = base + "\n\n" + "第二段还很长。" * 40 + "\n\n"
    blocks = [{"type": "text", "text": text}]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True

    v._page.js_calls.clear()
    v._needs_full_render = False
    blocks[0]["text"] = text + "继续写第三段。"
    assert v._perform_update_v2() is True
    js = _all_js(v)
    assert "_v2LiveSplit(\"content-placeholder\"" in js, "闭合段必须走切片正式化"
    assert v._v2_live_formalized_len > 0
    assert v._v2_live_formalized_len <= len(blocks[0]["text"])

def test_slice_never_cuts_inside_unclosed_fence(monkeypatch):
    """未闭合代码围栏：切点必须在 fence 之前，半截代码块不能当闭合段渲染。"""
    text = _long_paragraph(600) + "\n\n```python\nprint('半截代码')\n"
    blocks = [{"type": "text", "text": text}]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True
    slice_to = v._v2_live_slice(blocks[0]["text"])
    assert slice_to <= blocks[0]["text"].index("```python"), "切点不得进入未闭合 fence"
    sliced = blocks[0]["text"][:slice_to]
    assert sliced.count("```") % 2 == 0, "切片后的 fence 必须成对"

def test_slice_never_cuts_inside_unclosed_think(monkeypatch):
    text = "<think>思考内容" * 80 + "\n\n还有更多"
    blocks = [{"type": "text", "text": text}]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True
    slice_to = v._v2_live_slice(blocks[0]["text"])
    assert slice_to <= blocks[0]["text"].index("<think>"), "切点不得进入未闭合 think"

def test_slice_returns_zero_for_no_boundary_text(monkeypatch):
    """无空行中文长段落：切不出闭合段（这也解释了为何还需要 B' 尾部行内渲染）。"""
    text = _long_paragraph(900)
    blocks = [{"type": "text", "text": text}]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True
    assert v._v2_live_slice(text) == v._v2_live_formalized_len

# ── B'. 尾部行内格式化：无空行长段落也能立刻变格式 ────────────────────

def test_tail_inline_renders_markdown_syntax(monkeypatch):
    """尾部含 markdown 语法时必须产出格式化的 HTML（不是源码字面）。"""
    v = _make_viewer(monkeypatch, [])
    html = v._v2_live_tail_html("这里有 **加粗** 和 `代码` 以及 [链接](https://x.test)", reasoning=False)
    assert html, "尾部行内渲染不得返回空"
    assert "<strong>" in html or "<b>" in html, "**加粗** 必须变成标签"
    assert "**加粗**" not in html, "不得保留 markdown 源码字面"

def test_tail_inline_skipped_for_reasoning(monkeypatch):
    """思考块不跑 markdown：保持纯文本口径（与既有尾态一致）。"""
    v = _make_viewer(monkeypatch, [])
    assert v._v2_live_tail_html("思考 **内容**", reasoning=True) == ""

# ── 思考块容器归属（曾致思考增量全丢）────────────────────────────────

def test_reasoning_delta_goes_to_tool_container(monkeypatch):
    """思考块的 live 增量必须发往 #tool-content，不能硬编码 content-placeholder。

    思考块的 home 是 HOME_TOOL（``_reasoning_home`` 简洁模式返回 tool），节点
    挂在 #tool-content 下。容器写错时 ``_v2LiveNode`` 在正文容器里查不到该 key
    → ``_v2LiveAppend`` 静默返回 false → 思考增量全部丢失，用户看到的是
    「思考停在首帧纯文本，后续增长完全不显示」。
    """
    reasoning = "思考内容很长的一段。" * 60  # > _LIVE_MIN_CHARS 走尾态
    blocks = [{"type": "reasoning", "content": reasoning}]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True

    v._page.js_calls.clear()
    v._needs_full_render = False
    blocks[0]["content"] = reasoning + "继续思考的下一句。"
    assert v._perform_update_v2() is True
    js = _all_js(v)
    assert "_v2LiveAppend('tool-content'," in js, "思考增量必须发往 #tool-content"
    assert "_v2LiveAppend('content-placeholder'," not in js, "思考增量不得发往正文容器"

def test_reasoning_live_append_marks_think_compact(monkeypatch):
    """思考增量的 JS 调用必须带 reasoning=true，供重建 live 节点时补 think-compact。"""
    reasoning = "思考内容很长的一段。" * 60
    blocks = [{"type": "reasoning", "content": reasoning}]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True

    v._page.js_calls.clear()
    v._needs_full_render = False
    blocks[0]["content"] = reasoning + "续写。"
    assert v._perform_update_v2() is True
    js = _all_js(v)
    assert "true);" in js, "思考增量调用必须传 reasoning=true"

def test_tool_container_has_live_text_style():
    """#tool-content 里的 .v2-live-text 必须有 pre-wrap 样式。

    思考尾态节点归工具区，而 .v2-live-text 的样式原先只写在
    ``#content-placeholder`` 选择器下 → 命不中工具区 → 模型输出的换行被
    HTML 折叠，整段思考挤成一长串（截图实证）。
    """
    src = __import__("pathlib").Path(__file__).resolve().parents[2] / "app" / "widgets" / "card_viewers.py"
    code = src.read_text(encoding="utf-8")
    i = code.find("#tool-content .v2-live-text {{")
    assert i > 0, "缺少 #tool-content .v2-live-text 样式规则"
    block = code[i : code.find("}}", i)]
    assert "pre-wrap" in block, "工具区 live 尾态必须保留换行（pre-wrap）"
    assert "font-style: italic" in block, "思考文本应与 .think-content 口径一致（斜体）"

def test_tail_refresh_is_throttled(monkeypatch):
    """降载：新增不足阈值且距上次渲染过近时不重渲（防 O(n²)）。"""
    v = _make_viewer(monkeypatch, [])
    text = "短文本 **加粗**"
    first = v._v2_live_tail_refresh(text)
    assert first, "首次必须产出"
    second = v._v2_live_tail_refresh(text + "再补两个字")
    assert second is None, "增量不足阈值时必须跳过"

def test_tail_refresh_emits_js(monkeypatch):
    """尾部行内 HTML 必须真实下发（_v2LiveTailHtml），否则格式不会变。"""
    text = _long_paragraph(900) + "**结尾加粗**"
    blocks = [{"type": "text", "text": text}]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True
    assert "_v2LiveTailHtml" in _all_js(v)

# ── C. 高度上报：揭示期间逐帧报高 ────────────────────────────────────

def test_reveal_js_declares_report_throttle():
    """_v2tw 队列必须带高度上报节流常量（40ms，与 v1 _twStep 同口径）。"""
    src = __import__("pathlib").Path(__file__).resolve().parents[2] / "app" / "widgets" / "card_viewers.py"
    code = src.read_text(encoding="utf-8")
    assert "REPORT_MS: 40" in code
    assert "reportHeightDebounced" in code
    # 揭示步进函数必须真的调用上报
    i = code.find("window._v2LiveStep = function")
    j = code.find("window._v2LiveFlush = function", i)
    assert i > 0 and j > i
    assert "reportHeightDebounced" in code[i:j], "帧级揭示必须逐帧上报高度"

# ── 骨架完整性 ───────────────────────────────────────────────────────

def test_skeleton_has_all_new_hooks():
    """新骨架必须注册全部新函数：缺一个就是静默失效（runJavaScript 无回调）。"""
    src = __import__("pathlib").Path(__file__).resolve().parents[2] / "app" / "widgets" / "card_viewers.py"
    code = src.read_text(encoding="utf-8")
    for fn in (
        "window._v2tw",
        "window._v2LiveStep",
        "window._v2LiveFlush",
        "window._v2LiveDrop",
        "window._v2LiveAppendNow",
        "window._v2LiveAppend",
        "window._v2LiveTailHtml",
        "window._v2LiveSplit",
    ):
        assert fn in code, f"骨架缺 {fn}"

def test_live_split_initializes_embedded_fences():
    """切片段插入后必须就地初始化图表/公式/插件 fence（与 patchBlocks 同款五连）。

    缺失时容器插进 DOM 却没人 init → 图表区空白到下一次整块正式化
    （用户可见「切出来的图表空一块」）。
    """
    src = __import__("pathlib").Path(__file__).resolve().parents[2] / "app" / "widgets" / "card_viewers.py"
    code = src.read_text(encoding="utf-8")
    i = code.find("window._v2LiveSplit = function")
    j = code.find("正式化换 key", i)
    assert i > 0 and j > i
    seg = code[i:j]
    for fn in ("_initEchartsIn", "renderMermaidBlocks", "renderKatexBlocks", "_runFenceAssets", "_initWidgets"):
        assert fn in seg, f"_v2LiveSplit 缺 {fn} 初始化"

# ── B''. 尾部行内渲染的图表围栏保护（2026-11-01）───────────────────────

def test_tail_inline_promotes_closed_chart_fence_past_wrap_limit():
    """超长 tail 跳过整页代码高亮，但已闭合的 echarts 围栏必须出容器。

    修复前：``_render_inline_tail`` 对 > ``_TAIL_CODE_WRAP_MAX_CHARS`` 的文本
    跳过 ``_wrap_code_blocks_with_copy_button_web``，echarts 围栏退化成
    ``<pre><code class="language-echarts">`` —— 用户可见「已渲染图表回退成代码」。
    """
    from app.widgets.card_render_core import _TAIL_CODE_WRAP_MAX_CHARS, _render_inline_tail

    chart = '```echarts\n{"series":[{"type":"bar","data":[1,2,3]}]}\n```'
    text = "前缀。" * (_TAIL_CODE_WRAP_MAX_CHARS // 3) + "\n\n" + chart
    assert len(text) > _TAIL_CODE_WRAP_MAX_CHARS
    html = _render_inline_tail(text)
    assert "echarts-container" in html, "超长 tail 的图表围栏必须仍产出容器"
    assert "language-echarts" not in html, "不得降级成代码块"


def test_tail_inline_promotes_unclosed_chart_fence_to_skeleton():
    """超长 tail 的未闭合图表围栏必须是骨架，不能裸露半截 JSON。"""
    from app.widgets.card_render_core import _TAIL_CODE_WRAP_MAX_CHARS, _render_inline_tail

    text = (
        "前缀。" * (_TAIL_CODE_WRAP_MAX_CHARS // 3)
        + '\n\n```echarts\n{"series": [{"type": "line", "data": [1, 2'
    )
    assert len(text) > _TAIL_CODE_WRAP_MAX_CHARS
    html = _render_inline_tail(text)
    assert "chart-streaming" in html, "半截图表围栏必须降级为骨架"
    assert "language-echarts" not in html, "不得裸露半截 JSON"


def test_tail_refresh_renders_only_unsliced_tail(monkeypatch):
    """切片推进后，尾部刷新只能渲染 ``formalized_len`` 之后的部分。

    修复前传的是整块 ``live_text``：已切成 ``.v2-live-part`` 的图表/正文会被
    live 尾再渲染一遍（同一图表两份），超长时那份还是代码块 —— 用户可见
    「渲染代码与图表并存」。
    """
    chart = '```echarts\n{"series":[{"type":"bar","data":[1,2,3]}]}\n```'
    head = "第一段标记内容。\n\n" + chart + "\n\n后续说明文字。\n\n"
    unclosed = '```echarts\n{"series": [{"type": "line", "data": [1, 2'
    # 首帧文本必须 > _LIVE_MIN_CHARS，否则第一批走常规渲染、live key 未建立
    blocks = [{"type": "text", "text": head + unclosed + "0123456789" * 200}]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True

    # 第二轮：闭合段切片（第一段 + 图表进 part），tail 只剩未闭合围栏
    v._page.js_calls.clear()
    v._needs_full_render = False
    blocks[0]["text"] = head + unclosed + "0123456789" * 280
    assert v._perform_update_v2() is True
    assert v._v2_live_formalized_len > 0, "闭合段必须切片推进"

    # 第三轮：尾部增长超过降载阈值，触发 _v2LiveTailHtml
    v._page.js_calls.clear()
    v._needs_full_render = False
    blocks[0]["text"] = head + unclosed + "0123456789" * 440
    assert v._perform_update_v2() is True
    js = _all_js(v)
    assert "_v2LiveTailHtml" in js
    tail_arg = js[js.index("_v2LiveTailHtml") :]
    assert "第一段标记内容" not in tail_arg, "已切片前缀不得进入 live 尾"
    assert "echart-" not in tail_arg, "已渲染图表不得在 live 尾重复"
