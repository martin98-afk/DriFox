# -*- coding: utf-8 -*-
"""回归测试：v2 长思考尾态必须保持「思考预览」的限高语义。

功能背景
--------
思考流式期间有两种 DOM 形态：

1. 短思考（< ``_LIVE_MIN_CHARS``）走常规渲染 → ``.think-streaming`` +
   ``.think-streaming-live``（CSS max-height: 96px，只看最新一段）。
2. 长思考（≥ ``_LIVE_MIN_CHARS``）走 v2 纯文本尾态 → ``.v2-live-text
   think-compact``，整段思考文本一次性进 DOM。

缺陷（2026-11-02 用户实测）
--------------------------
v2 尾态此前**没有任何高度约束**：切到别的标签页再切回时 showEvent 补渲会
首次启用尾态，思考块从 96px 窗口变成整段全高显示（卡片被撑高、外层聊天
列表被顶走）。修复后尾态与 ``.think-streaming-live`` 同口径限高，追加文本
后窗口滚到底部显示最新内容。

关键约束
--------
1. 限高规则必须同时覆盖两个容器（简洁模式思考 home=tool-content，非简洁
   模式 home=content-placeholder）。
2. 增量追加（``_v2LiveAppendNow``）与切片重建（``_v2LiveSplit``）后必须
   滚底：overflow-y:hidden 的限高窗口不滚底就停在最旧的一段。
3. 切片路径必须把 reason 归属容器与 ``think-compact`` 类传下去：漏传会把
   思考内容插进正文容器、尾态退化成普通正文样式（限高选择器命不中）。
"""

from __future__ import annotations

import json as stdlib_json
import re
import sys
from typing import Any, Dict, List

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


class _StubPage:
    def __init__(self):
        self.js_calls: List[str] = []

    def runJavaScript(self, js_code, callback=None):
        self.js_calls.append(js_code)


def _build_skeleton_html() -> str:
    """用桩 page 构建真实骨架 HTML（不拉 WebEngine）。"""
    _ensure_qapp()
    from app.widgets.card_viewers import CodeWebViewer

    page = _StubPage()
    captured = {}
    v = CodeWebViewer.__new__(CodeWebViewer)
    v._light_skeleton = True
    v._streaming = False
    v._v2_last = None
    v.page = lambda: page
    v.setHtml = lambda html, url=None: captured.setdefault("html", html)
    v.ensure_transparent_composition = lambda: None
    v._load_skeleton()
    return captured.get("html", "")


# ── 1. 骨架 CSS：思考尾态限高必须存在，且两个容器都覆盖 ──────────────
def test_v2_reasoning_live_has_height_cap():
    html = _build_skeleton_html()
    m = re.search(
        r"#content-placeholder \.v2-live-text\.think-compact,\s*"
        r"#tool-content \.v2-live-text\.think-compact \{(.*?)\}",
        html,
        re.S,
    )
    assert m, "骨架缺少思考尾态限高规则（.v2-live-text.think-compact）"
    rule = m.group(1)
    assert "max-height: 96px" in rule, f"思考尾态未限高到 96px: {rule!r}"
    assert "overflow-y: hidden" in rule, f"限高窗口必须隐藏滚动条（内滚由 JS 维护）: {rule!r}"
    # 顶部渐隐与 .think-streaming-live 同口径
    assert "mask-image" in rule, f"思考尾态缺少顶部渐隐: {rule!r}"


# ── 2. 骨架 JS：增量追加后 reasoning 尾态滚到底 ──────────────────────
def test_v2_live_append_scrolls_reasoning_to_bottom():
    html = _build_skeleton_html()
    assert "if (reasoning) live.scrollTop = live.scrollHeight;" in html, (
        "思考尾态追加后未滚底：限高窗口会一直停在最旧的一段"
    )


# ── 3. 骨架 JS：切片重建节点保留 think-compact 类并滚底 ──────────────
def test_v2_live_split_keeps_reasoning_class():
    html = _build_skeleton_html()
    assert "window._v2LiveSplit = function (containerId, key, html, tailText, tailIsHtml, reasoning)" in html, (
        "切片通道缺 reasoning 参数：重建的 live 节点会丢 think-compact 类"
    )


# ── 4. Python 切片路径：reasoning 归属容器与 reasoning 标志正确 ──────
def _make_slice_viewer(monkeypatch, compact: bool):
    _ensure_qapp()
    from app.widgets.card_viewers import CodeWebViewer
    from app.widgets.render import BlockCache, RenderCtx

    page = _StubPage()
    v = CodeWebViewer.__new__(CodeWebViewer)
    v._page = page
    v._v2_live_formalized_len = 0
    v._v2_live_key = ""
    v._v2_live_sent = ""
    v._v2_live_whole_done_len = 0
    v._height_report_pending = False
    v._last_rendered_markdown = ""
    v._markdown_text = "md"
    v._block_cache = BlockCache()
    monkeypatch.setattr(CodeWebViewer, "_tool_compact_mode", compact)
    monkeypatch.setattr(CodeWebViewer, "page", lambda self: self._page)
    monkeypatch.setattr(CodeWebViewer, "_v2_render_ctx", lambda self: RenderCtx(streaming=True, compact=compact))
    return v


def _slice_js_args(js: str) -> Dict[str, Any]:
    """解析骨架切片的 _v2LiveSplit 调用参数（头部）与容器 id。"""
    m = re.search(
        r"window\._v2LiveSplit\(\"([^\"]+)\","
        r"(\"[^\"]*\"),"
        r"(\"[^\"]*\"),"
        r"(\"[^\"]*\"),"
        r"(true|false),"
        r"(true|false)\);",
        js,
    )
    assert m, f"未发出 _v2LiveSplit 调用: {js!r}"
    return {
        "container": m.group(1),
        "key": stdlib_json.loads(m.group(2)),
        "seg_html": stdlib_json.loads(m.group(3)),
        "tail": stdlib_json.loads(m.group(4)),
        "tail_is_html": m.group(5) == "true",
        "reasoning": m.group(6) == "true",
    }


def test_slice_reasoning_goes_to_tool_container_with_flag(monkeypatch):
    """简洁模式：思考切片必须落在 tool-content 且带 reasoning 标志。

    旧实现硬编码 content-placeholder + reasoning=False：思考被插进正文容器，
    限高规则（按容器前缀 + think-compact 类）全部命不中。
    """
    v = _make_slice_viewer(monkeypatch, compact=True)
    blocks = [{"type": "reasoning", "content": "第一段思考。\n\n第二段思考。"}]
    ok = v._emit_v2_live_slice(blocks, v._v2_render_ctx(), 0, "live:0", blocks[0]["content"], 8, True)
    assert ok is True
    assert v._page.js_calls, "切片未下发 JS"
    args = _slice_js_args(v._page.js_calls[-1])
    assert args["container"] == "tool-content", f"思考切片落错容器: {args['container']}"
    assert args["reasoning"] is True, "思考切片丢了 reasoning 标志"


# ── 5. v2 尾态 HTML 形态：思考带 think-compact，正文不带 ─────────────
def test_v2_live_html_reasoning_class():
    from app.widgets.card_viewers import CodeWebViewer

    assert "think-compact" in CodeWebViewer._v2_live_html("思考", reasoning=True)
    assert "think-compact" not in CodeWebViewer._v2_live_html("正文", reasoning=False)


# ── 6. 思考尾态不得切片：part 是不限高的正式块，会把思考逐段铺开 ──────
def test_reasoning_live_never_slices():
    import inspect

    from app.widgets.card_viewers import CodeWebViewer

    src = inspect.getsource(CodeWebViewer._perform_update_v2)
    assert "and not live_reasoning" in src, (
        "思考活动块仍会走前缀切片：.v2-live-part 不受预览限高约束，"
        "思考内容会逐段全量铺开（限高窗口失效）"
    )
