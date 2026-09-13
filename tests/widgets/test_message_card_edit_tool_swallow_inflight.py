# -*- coding: utf-8 -*-
"""回归测试：编辑类工具完成时若有在途异步渲染，落地会吞掉完成框（永久消失）。

根因（时序链）：
  1. 非流式（流式已结束）+ `_markdown_text` 超 `_ASYNC_HISTORY_RENDER_MIN_CHARS`
     → `_perform_update` 非流式分支走 `_sequence_render`（线程池）；快照 md 不含
     尚未完成的工具 A。
  2. A（编辑类）完成 → `append_tool_result` 编辑分支：只做 JS 增量注入（完成框进
     DOM）+ 重设 `_lazy_markdown_cb`，**不触发渲染、不递增 `_render_seq`**
     → 在途渲染不会被作废。
  3. 在途结果落地 `_apply_render_result`：seq 未过期 → `_needs_save_restore`
     （`_restore_finished_ids` 已含 A）→ save 把 DOM 中 A 的完成框 `el.remove()` →
     `updateContent(旧快照 HTML，不含 A)` → restore 判定 A 已 finished → 不恢复
     → 完成框永久消失。

修复：编辑分支追加 `viewer.invalidate_inflight_render()`（递增 seq + 清 pending），
在途旧快照过期丢弃；非编辑工具行为不变（`_schedule_render(immediate=True)`
新快照自带该工具块）。

验证方式：桩 viewer 绑定 CodeWebViewer 真实渲染逻辑（`_perform_update` /
`_sequence_render` / `_apply_render_result` / `_build_save_and_restore_js`），
线程池换成本地假 pool 以确定性控制"在途"时序。
"""

import json
import re
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

# Qt 属性必须先于 message_card（顶层拉入 QWebEngineView）设置，否则 native crash
QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
# QApplication 也必须在导入 message_card 之前存在（qfluentwidgets 的 qconfig 在
# 无 QApplication 时创建会拿到无效 C++ 对象，后续构造 QWidget 直接 RuntimeError）
_APP = QApplication.instance() or QApplication(sys.argv)

try:
    from PySide6.QtWebEngineWidgets import (  # noqa: F401
        QWebEnginePage,
        QWebEngineSettings,
        QWebEngineView,
    )
except Exception:
    pass

from app.widgets import message_card as mc  # noqa: E402
from app.widgets.message_card import CodeWebViewer, MessageCard  # noqa: E402

TOOL_ID = "call_edit_inflight"


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


class _FakePage:
    def __init__(self):
        self.calls = []

    def runJavaScript(self, js, cb=None):
        self.calls.append(js)
        if cb is not None:
            try:
                cb(None)
            except Exception:
                pass


class _FakeFuture:
    def __init__(self):
        self._cbs = []
        self.html = None

    def add_done_callback(self, cb):
        self._cbs.append(cb)

    def result(self):
        return self.html

    def complete(self, html):
        self.html = html
        for cb in list(self._cbs):
            cb(self)


class _FakePool:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, snapshot):
        fut = _FakeFuture()
        self.jobs.append((fn, snapshot, fut))
        return fut


_VIEWER_CLS = None


def _viewer_cls():
    """绑定真实渲染逻辑的 viewer 桩类。"""
    global _VIEWER_CLS
    if _VIEWER_CLS is not None:
        return _VIEWER_CLS

    class _ViewerStub:
        _perform_update = CodeWebViewer._perform_update
        _apply_render_result = CodeWebViewer._apply_render_result
        _sequence_render = CodeWebViewer._sequence_render
        _collect_render_snapshot = CodeWebViewer._collect_render_snapshot
        _build_save_and_restore_js = CodeWebViewer._build_save_and_restore_js
        _on_render_done_signal = CodeWebViewer._on_render_done_signal
        _clear_tool_dom_dirty_guarded = CodeWebViewer._clear_tool_dom_dirty_guarded
        _push_unrendered_tail_text = CodeWebViewer._push_unrendered_tail_text
        invalidate_inflight_render = CodeWebViewer.invalidate_inflight_render

        _ASYNC_HISTORY_RENDER_MIN_CHARS = CodeWebViewer._ASYNC_HISTORY_RENDER_MIN_CHARS

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

        # ── 桩替换 ──
        def page(self):
            return self._page

        def parent(self):
            return None

        def isVisible(self):
            return True

        def _refresh_viewer_font_css(self):
            pass

        def _schedule_render(self, immediate=False):
            pass

        def _append_text_incremental(self, text):
            pass

        def _report_height(self):
            pass

    _VIEWER_CLS = _ViewerStub
    return _ViewerStub


def _make_card(monkeypatch):
    _ensure_qapp()
    monkeypatch.setattr(mc, "_edit_tools", lambda: frozenset({"edit", "write", "multi_edit"}))
    monkeypatch.setattr(mc.sip, "isdeleted", lambda o: False)
    card = MessageCard(role="assistant")
    card._lazy_rendered = True
    card.viewer = _viewer_cls()()
    card._streaming = False
    card._content_data = []
    return card


def _analyze_landing_js(js: str):
    """提取落地 JS 的判据：(finishedSet, updateContent 的 HTML)。"""
    m = re.search(r"_finishedSet=(\[.*?\]);", js)
    finished = json.loads(m.group(1)) if m else []
    html = ""
    idx = js.find("updateContent(")
    if idx != -1:
        try:
            html, _ = json.JSONDecoder().raw_decode(js[idx + len("updateContent(") :])
        except Exception:
            html = ""
    return finished, html


def _submit_inflight_render(card, pool, monkeypatch):
    """制造「长正文 + 非流式」的在途异步渲染，返回提交时的 seq。"""
    monkeypatch.setattr(mc, "_RENDER_POOL", pool)
    card.viewer._markdown_text = "正文" * 4000  # > 6000 字符阈值
    card.viewer._perform_update()
    assert pool.jobs, "长正文的非流式渲染应走线程池（异步）"
    return card.viewer._render_seq


def test_inflight_render_would_have_swallowed_edit_tool_block(monkeypatch):
    """契约：旧快照落地确实会造成吞框（save 移除 + restore 跳过 + HTML 不含块）。

    该断言锁定"必须作废在途渲染"的理由——若未来有人放宽 restore 判据或改用
    其它保护，这里会先失败提醒重新评估。
    """
    card = _make_card(monkeypatch)
    viewer = card.viewer
    seq = _submit_inflight_render(card, _FakePool(), monkeypatch)

    viewer._on_render_done_signal(seq, "<p>旧快照内容</p>")
    landed = [c for c in viewer._page.calls if "updateContent(" in c]
    assert landed, "未作废时，在途结果应落地派发 updateContent"
    finished, html = _analyze_landing_js(landed[-1])
    assert f'data-tool-call-id="{TOOL_ID}"' not in html, "旧快照 HTML 本就不含该工具块"
    assert html, "落地 JS 应携带 updateContent 的 HTML 参数"


def test_edit_tool_result_invalidates_inflight_render(monkeypatch):
    """编辑工具完成后：在途异步渲染被作废，旧结果落地不再派发 updateContent。"""
    pool = _FakePool()
    card = _make_card(monkeypatch)
    viewer = card.viewer
    seq = _submit_inflight_render(card, pool, monkeypatch)

    card.append_tool_result(
        tool_name="edit",
        arguments={"path": "a.py", "oldString": "x", "newString": "y"},
        result="ok",
        success=True,
        tool_call_id=TOOL_ID,
    )
    injected = [c for c in viewer._page.calls if f'data-tool-call-id="{TOOL_ID}"' in c]
    assert injected, "编辑工具完成应通过 JS 注入完成框"
    assert viewer._render_seq > seq, "编辑工具完成后必须作废在途异步渲染（递增 _render_seq）"
    assert viewer._render_pending is None, "作废时须一并清空 pending 快照"

    before = len(viewer._page.calls)
    viewer._on_render_done_signal(seq, "<p>旧快照内容</p>")
    landed = [c for c in viewer._page.calls[before:] if "updateContent(" in c]
    assert not landed, "被作废的在途结果不得落地（否则 save/restore 会吞掉完成框）"


def test_edit_tool_result_keeps_lazy_cb_and_no_immediate_render(monkeypatch):
    """修复不改变编辑工具既有设计：重设懒回调，但不触发即时渲染（防闪烁）。"""
    card = _make_card(monkeypatch)
    viewer = card.viewer
    renders = {"count": 0}
    viewer._schedule_render = lambda immediate=False: renders.__setitem__("count", renders["count"] + 1)

    card.append_tool_result(
        tool_name="edit",
        arguments={"path": "b.py", "oldString": "p", "newString": "q"},
        result="ok",
        success=True,
        tool_call_id="call_edit_2",
    )
    assert viewer._lazy_markdown_cb is not None, "编辑工具完成必须重设懒回调（停止/终渲染靠它刷新 md）"
    assert renders["count"] == 0, "编辑工具不应触发即时渲染（防闪烁设计）"


def test_non_edit_tool_result_still_schedules_render(monkeypatch):
    """非编辑工具行为不变：走 _schedule_render(immediate=True) 提交含块的新快照。"""
    card = _make_card(monkeypatch)
    viewer = card.viewer
    calls = []
    viewer._schedule_render = lambda immediate=False: calls.append(immediate)

    card.append_tool_result(
        tool_name="read",
        arguments={"path": "c.py"},
        result="file content",
        success=True,
        tool_call_id="call_read_1",
    )
    assert calls and calls[0] is True, "非编辑工具完成应触发即时渲染调度"
    assert viewer._lazy_markdown_cb is not None


def test_invalidate_inflight_render_is_noop_without_inflight(monkeypatch):
    """无在途渲染时作废调用是 no-op（不空转递增 seq，避免误伤后续渲染）。"""
    card = _make_card(monkeypatch)
    viewer = card.viewer
    seq = viewer._render_seq
    viewer.invalidate_inflight_render()
    assert viewer._render_seq == seq


def test_restore_judgement_is_dom_presence(monkeypatch):
    """restore 判据必须是「DOM 存在性」，不得以 finished 为前置。

    根因族：旧判据「finished 则不恢复」假设 markdown 一定会重建该块，把「是否恢复」
    与「HTML 是否真的含该块」解耦——任何导致 HTML 缺块的路径（在途旧快照落地、
    懒回调未刷新、注入失败…）都会让工具块被 save 移除后无人恢复，永久消失。
    """
    _make_card(monkeypatch)  # 触发 QApplication + message_card 导入
    from app.widgets.message_card import CodeWebViewer

    viewer = _viewer_cls()()
    js = CodeWebViewer._build_save_and_restore_js(viewer, "<p>html</p>", finished_ids={"done_x"})
    assert "if(!document.querySelector('[data-tool-call-id=\"'+b.id+'\"]')){" in js, (
        "restore 必须以 DOM 是否存在同 id 块为判据"
    )
    assert "if(!_isFinished&&" not in js, "判据不得再前置 !_isFinished（否则 HTML 缺块时块永久消失）"
    assert '"done_x"' in js, "已完成集合仍需注入（供 data-restored-finished 排查标记）"
    assert "data-restored-finished" in js, "恢复的已完成块应带排查标记，便于定位来源"
