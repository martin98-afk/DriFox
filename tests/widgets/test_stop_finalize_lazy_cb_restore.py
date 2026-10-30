# -*- coding: utf-8 -*-
"""回归测试：强停（中途打断）后正文丢失 + 工具完成框无法展开。

根因（时序链，2026-10-30 实测复现，探针
tests/debug/_probe_stop_loss_and_tool_body.py）：
  1. 流式期间每拍渲染消费 ``_lazy_markdown_cb`` 后置 None（card_viewers
     ``_perform_update`` 流式分支）。
  2. 用户点停止 → ``finish_streaming``：简洁模式 + 有已完成工具/未闭合
     think 时终渲染 defer（``DOCK_COLLAPSE_SETTLE_MS=280ms``）。
  3. 紧随其后的 ``_cleanup_render_cache`` 清空 ``_markdown_text=""``——
     此时懒回调仍是 None，恢复途径断绝。
  4. 280ms 后终渲染：v1 非流式分支拿空 md → 渲染空 HTML →
     ``updateContent('')`` 清空 #content-placeholder；工具完成框在
     #tool-content 不受影响 → **卡片只剩工具框**。
     v2 则直接短路 return（DOM 保留，但完成态重排未做）。
  5. 同一时点点击懒 body 完成框 → ``_serve_tool_body`` 双源
     （_markdown_text / _tool_md_cache）全空 → 回空 html → JS
     ``_fillToolBody`` 空分支直接 return（不展开、不清 lazyBody，
     2s 兜底又被 lazyLoading 删除杀死）→ **永远展不开**。

修复：
  - ``_cleanup_render_cache`` 清空 _markdown_text 时从父卡重建懒回调
    （与 _v2_blocks 同款父链遍历）；
  - ``_serve_tool_body`` 反查源为空时走懒回调恢复（同 get_plain_text 口径）；
  - 骨架 JS ``_fillToolBody`` 空 html 分支清 lazyBody（v65）。

本测试用 stub viewer 绑定真实 CodeWebViewer 方法验证 Python 侧不变量
（与 test_message_card_edit_tool_stop_swallow.py 风格一致）。
"""

import re
import sys
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
try:
    from PyQt5.QtWebEngineWidgets import (  # noqa: F401
        QWebEnginePage,
        QWebEngineSettings,
        QWebEngineView,
    )
except Exception:
    pass

from app.widgets.card_render_core import _SKELETON_CACHE_VERSION


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


class _StubPage:
    """记录 runJavaScript 调用"""

    def __init__(self):
        self.js_calls = []

    def runJavaScript(self, js_code, callback=None):
        self.js_calls.append(js_code)


class _StubViewer:
    """CodeWebViewer 桩：cleanup / tool-body 路径所需最小接口（无 WebEngine）"""

    def __init__(self):
        self._streaming = True
        self._render_seq = 0
        self._render_pending = None
        self._final_render_seq = 0
        self._final_render_inflight = False
        self._needs_full_render = False
        self._stable_html = ""
        self._stable_md_len = 0
        self._last_rendered_html = None
        self._last_rendered_markdown = ""
        self._markdown_text = ""
        self._tail_last_rendered_ts = 0.0
        self._tail_last_rendered_len = 0
        self._lazy_markdown_cb = None
        self._finished_tool_fingerprints = set()
        self._tool_md_cache = {}
        self._is_js_ready = True
        self._tool_compact_mode = True
        self._tool_target_id = "tool-content"
        self._tool_lazy_body = True
        self._page = _StubPage()
        self._parent = None

    def parent(self):
        return self._parent

    def page(self):
        return self._page

    def _schedule_render(self, immediate=False):
        pass

    def _serve_tool_body(self, tool_call_id):
        # 绑定真实实现：验证懒回调兜底逻辑本身
        from app.widgets.card_viewers import CodeWebViewer

        CodeWebViewer._serve_tool_body(self, tool_call_id)


def _make_card_with_stub():
    from app.widgets.message_card import MessageCard

    _ensure_qapp()
    card = MessageCard(role="assistant")
    card._lazy_rendered = True
    card.viewer = _StubViewer()
    card.viewer._parent = card
    return card


# ─────────────────────────────────────────────
# 1. cleanup 清空 _markdown_text 后懒回调必须可用
# ─────────────────────────────────────────────

def test_cleanup_restores_lazy_cb_when_consumed():
    """流式末拍已消费懒回调（None）时，cleanup 必须从父卡重建。

    旧行为：cleanup 清 _markdown_text 且懒回调保持 None → defer 终渲染拿
    空 md → 渲染空 HTML 清掉正文（强停正文丢失根因）。
    """
    from app.widgets.card_viewers import CodeWebViewer

    card = _make_card_with_stub()
    v = card.viewer

    card.append_text("强停前已渲染的正文内容，停止后不应消失。\n\n第二段。")
    # 模拟流式末拍渲染已消费懒回调
    v._lazy_markdown_cb = None

    CodeWebViewer._cleanup_render_cache(v)

    assert v._lazy_markdown_cb is not None, (
        "_cleanup_render_cache 清空 _markdown_text 后懒回调为 None 时必须从父卡重建"
        "——否则 defer 终渲染拿空 md 渲染空 HTML，正文被 updateContent('') 清掉"
    )
    assert v._markdown_text == "", "cleanup 仍应清空 _markdown_text（内存语义不变）"


def test_restored_cb_regenerates_full_content():
    """重建的懒回调生成的 md 必须含停止前全部正文。"""
    from app.widgets.card_viewers import CodeWebViewer

    card = _make_card_with_stub()
    v = card.viewer

    card.append_text("第一段可见正文 TOKEN_A。\n\n第二段 TOKEN_B。")
    v._lazy_markdown_cb = None
    CodeWebViewer._cleanup_render_cache(v)

    md = v._lazy_markdown_cb()
    assert "TOKEN_A" in md and "TOKEN_B" in md, "重建回调应产出完整正文 md"


def test_cleanup_keeps_existing_cb():
    """懒回调仍存在时 cleanup 不得覆盖（保持原引用，行为不变）。"""
    from app.widgets.card_viewers import CodeWebViewer

    card = _make_card_with_stub()
    v = card.viewer

    card.append_text("内容。")
    original = v._lazy_markdown_cb
    assert original is not None
    CodeWebViewer._cleanup_render_cache(v)
    assert v._lazy_markdown_cb is original, "已有懒回调不应被重建逻辑覆盖"


# ─────────────────────────────────────────────
# 2. _serve_tool_body 反查源为空时走懒回调兜底
# ─────────────────────────────────────────────

def test_serve_tool_body_falls_back_to_lazy_cb():
    """cleanup 清空 _markdown_text 后点击完成框，必须经懒回调恢复反查。

    旧行为：双源全空 → 回空 html → JS _fillToolBody 不展开且 2s 兜底失效
    → 完成框永远展不开。
    """
    from app.widgets.card_viewers import CodeWebViewer

    card = _make_card_with_stub()
    v = card.viewer
    tid = "call_stop_1"

    card.update_tool_streaming(tool_call_id=tid, tool_name="bash", partial_args={"command": "echo hi"})
    card.append_tool_result(
        tool_name="bash",
        arguments={"command": "echo hi"},
        result="hi\n",
        success=True,
        tool_call_id=tid,
    )
    # 强停收尾：清缓存（对齐 finish_streaming + _cleanup_render_cache 实际效果）
    v._tool_md_cache.clear()
    v._markdown_text = ""
    v._lazy_markdown_cb = None
    CodeWebViewer._cleanup_render_cache(v)
    assert v._lazy_markdown_cb is not None

    # 终渲染落地前的点击窗口（_markdown_text 仍空）
    v._serve_tool_body(tid)

    assert v._page.js_calls, "_serve_tool_body 必须回填 _fillToolBody"
    js = v._page.js_calls[-1]
    assert "_fillToolBody" in js, f"应调用 _fillToolBody: {js[:120]}"
    assert tid in js, "回填应携带 tool_call_id"
    # payload 非空：b64 解码后应含工具名（空 html 会让 JS 直接 return 不展开）
    import base64

    m = re.search(r'_fillToolBody\("[^"]+",\s*"([A-Za-z0-9+/=]*)"\)', js)
    assert m, f"未解析到 b64 payload: {js[:200]}"
    payload = base64.b64decode(m.group(1)).decode("utf-8", errors="replace")
    assert payload.strip(), "payload 不得为空（空 html → 完成框永远展不开）"
    assert "echo hi" in payload, "payload 应含工具内容（反查恢复成功）"


def test_serve_tool_body_recovers_markdown_text():
    """懒回调兜底恢复的 md 应回写 _markdown_text（后续请求/终渲染复用）。"""
    from app.widgets.card_viewers import CodeWebViewer

    card = _make_card_with_stub()
    v = card.viewer
    tid = "call_stop_2"

    card.update_tool_streaming(tool_call_id=tid, tool_name="read", partial_args={"path": "x.py"})
    card.append_tool_result(
        tool_name="read",
        arguments={"path": "x.py"},
        result="data",
        success=True,
        tool_call_id=tid,
    )
    v._tool_md_cache.clear()
    v._markdown_text = ""
    v._lazy_markdown_cb = None
    CodeWebViewer._cleanup_render_cache(v)

    v._serve_tool_body(tid)
    assert v._markdown_text.strip(), "兜底恢复后应回写 _markdown_text"
    assert tid in v._markdown_text, "回写的 md 应含工具块（供后续反查）"


# ─────────────────────────────────────────────
# 3. 骨架 JS：_fillToolBody 空 html 分支清 lazyBody（防无限重试）
# ─────────────────────────────────────────────

def test_fill_tool_body_empty_clears_lazy_body_marker():
    """空 html 时必须清 lazyBody 标记：留着会无限重试空请求且 2s 兜底已失效。"""
    from app.widgets import card_viewers

    src = Path(card_viewers.__file__).read_text(encoding="utf-8")
    m = re.search(
        r"window\._fillToolBody = function \(callId, b64\) \{\{(.*?)\n\s*\}\};",
        src,
        re.S,
    )
    assert m, "未找到 _fillToolBody 定义"
    body = m.group(1)
    empty_branch = re.search(r"if \(!html\) \{\{(.*?)\}\}", body, re.S)
    assert empty_branch, "未找到空 html 分支"
    assert "lazyBody" in empty_branch.group(1), (
        "空 html 分支应清除 lazyBody 标记（否则下次点击仍走空请求循环，永远展不开）"
    )


def test_skeleton_cache_version_bumped():
    """改骨架 JS 必须递增 _SKELETON_CACHE_VERSION（本次 v65：_fillToolBody 空分支）。"""
    assert _SKELETON_CACHE_VERSION >= 65, (
        f"_SKELETON_CACHE_VERSION={_SKELETON_CACHE_VERSION}，改 _fillToolBody 后必须 ≥65"
    )
