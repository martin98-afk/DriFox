# -*- coding: utf-8 -*-
"""[T18/T11] 回归：reset_for_reuse 必须作废在途异步渲染（跨会话内容污染缺口）。

时序链：
  1. 长消息非流式渲染提交线程池（在途，结果未落地）；
  2. 切会话 → viewer 归还复用（``reset_for_reuse``）；
  3. 旧渲染结果落地 ``_apply_render_result``。

修复前 ``reset_for_reuse`` 不清 ``_render_seq/_render_inflight/_render_pending``：
旧 seq 未过期 → 旧 HTML 快照打到复用后的新卡片（跨会话内容污染）。
修复后：reset 递增 seq + 复位 inflight/pending → 落地时 seq 守卫直接丢弃。
"""

import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

# Qt 属性必须先于 card_viewers（顶层拉入 QWebEngineView）设置，否则 native crash
QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
_APP = QApplication.instance() or QApplication(sys.argv)

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView  # noqa: F401
except Exception:
    pass

from app.widgets import card_viewers as cv  # noqa: E402
from app.widgets.card_viewers import CodeWebViewer  # noqa: E402


class _FakePage:
    def __init__(self):
        self.calls = []

    def runJavaScript(self, js, cb=None):
        self.calls.append(js)


class _FakeFuture:
    def __init__(self):
        self._cbs = []

    def add_done_callback(self, cb):
        self._cbs.append(cb)

    def result(self):
        return None


class _FakePool:
    """本地假线程池：只登记任务不执行，确定性控制「在途未落地」时序。"""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, snapshot):
        fut = _FakeFuture()
        self.jobs.append((fn, snapshot, fut))
        return fut


class _ViewerStub:
    """绑定真实渲染/复用逻辑的 viewer 桩（形态对齐 test_message_card_edit_tool_swallow_inflight）。"""

    reset_for_reuse = CodeWebViewer.reset_for_reuse
    _sequence_render = CodeWebViewer._sequence_render
    _apply_render_result = CodeWebViewer._apply_render_result
    _collect_render_snapshot = CodeWebViewer._collect_render_snapshot

    def __init__(self):
        self._page = _FakePage()
        self._markdown_text = ""
        self._streaming = False
        self._is_history = False
        self._is_js_ready = True
        self._final_render_pending = False
        self._last_rendered_html = ""
        self._last_rendered_markdown = ""
        self._processed_md_hash = None
        self._cached_streaming_html = None
        self._cached_raw_md_hash = 0
        self._lazy_markdown_cb = None
        self._tool_md_cache = {}
        self._tool_dom_dirty = False
        self._tool_dom_dirty_gen = 0
        self._injected_pending_tools = set()
        self._render_seq = 0
        self._render_inflight = False
        self._render_pending = None
        self._render_deferred = False
        self._theme_css_pending = False
        self._stable_html = ""
        self._stable_md_len = 0
        self._tail_html_hash = 0
        self._needs_full_render = True
        self._light_skeleton = False
        self._min_render_interval = 80
        self._height_report_pending = False
        self._thinking_finalized = False
        self._restore_finished_ids = set()
        self._finish_t0 = 0.0
        self._tool_compact_mode = False
        self._tool_target_id = "tool-content"
        self._last_chunk_time = 0.0
        self._current_adaptive_interval = 300
        self._context_lost = False
        self._render_crash_count = 0
        self._webgl_ctx_lost_count = 0
        self._document_height = 0
        self._body_client_height = 0
        self._body_scroll_top = 0
        self._body_geom_valid = False

    # ── 桩替换 ──
    def page(self):
        return self._page

    def setMinimumHeight(self, h):
        pass

    def setFixedHeight(self, h):
        pass


def _make_viewer(monkeypatch):
    _ensure = QApplication.instance() or QApplication(sys.argv)
    assert _ensure is not None
    monkeypatch.setattr(cv, "_RENDER_POOL", _FakePool())
    return _ViewerStub()


def test_reset_for_reuse_invalidates_inflight(monkeypatch):
    """提交不落地 → reset_for_reuse → seq 递增/inflight False/pending None → 旧结果落地被丢弃。"""
    viewer = _make_viewer(monkeypatch)

    # 1) 提交不落地：假池只登记不执行，在途状态真实产生
    viewer._sequence_render("<p>很长的一段消息内容</p>", False)
    assert viewer._render_inflight is True, "FakePool 下提交应处于在途态"
    old_seq = viewer._render_seq
    assert old_seq >= 1

    # 2) 归还复用：必须作废在途渲染
    viewer.reset_for_reuse()
    assert viewer._render_seq == old_seq + 1, "reset 必须递增 seq 使在途结果过期"
    assert viewer._render_inflight is False, "reset 必须复位在途标志"
    assert viewer._render_pending is None, "reset 必须清空积压快照"

    # 3) 旧任务结果落地：seq 守卫丢弃，不得应用到复用后的新卡
    viewer._apply_render_result(old_seq, "<p>old content</p>")
    assert viewer._last_rendered_html != "<p>old content</p>", "旧渲染结果不得落到新卡片"
    assert viewer._last_rendered_html is None, "落地被丢弃后不应有任何 HTML 应用（reset 清理基线为 None）"
