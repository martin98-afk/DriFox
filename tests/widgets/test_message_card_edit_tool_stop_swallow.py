# -*- coding: utf-8 -*-
"""回归测试：编辑类工具完成后短时间内停止对话，完成框被吞（永久消失）。

根因（时序链）：
  1. append_tool_result 编辑类工具分支只做 JS 注入 + _tool_md_cache 缓存，
     **不重设 viewer._lazy_markdown_cb 也不渲染**（防闪烁设计）。
  2. 此前最后一次流式渲染已消费 _lazy_markdown_cb（置 None），
     _markdown_text 停在旧值（不含新完成的工具块）。
  3. 用户此时点停止 → card.finish_streaming → viewer.finish_streaming →
     _perform_update 非流式分支：cb=None → _markdown_text 旧值渲染 →
     HTML 不含编辑工具完成块。
  4. save/restore：save 把 DOM 完成框 el.remove()；restore 判定 tid 已入
     _restore_finished_ids → 不恢复（防重复设计）。
  5. 完成框被吞且 md 永不含它 → 永久消失。

正常流程靠"工具完成后模型继续 append_text → 重设 cb"兜底，因此只在
"编辑完成 → 下一段文本"窗口内停止才触发——与用户描述"完成编辑的短时间内
暂停对话偶尔吞掉完成框"吻合。

修复：append_tool_result 编辑分支也重设 _lazy_markdown_cb（不触发渲染，
保持"跳过即时渲染防闪烁"设计不变）。

本测试用 stub viewer 验证 Python 侧状态机不变量（与
tests/widgets/test_message_card_tool_box_disappear.py 风格一致）。
"""

import sys

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


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


class _StubPage:
    """记录 runJavaScript 调用与回调"""

    def __init__(self):
        self.js_calls = []

    def runJavaScript(self, js_code, callback=None):
        self.js_calls.append(js_code)


class _StubViewer:
    """CodeWebViewer 桩：编辑工具完成路径所需的最小接口（无 WebEngine）"""

    def __init__(self):
        self._streaming = True
        self._restore_finished_ids = set()
        self._tool_target_id = "tool-content"
        self._tool_compact_mode = False
        self._tool_dom_dirty = False
        self._tool_dom_dirty_gen = 0
        self._injected_pending_tools = set()
        self._tool_md_cache = {}
        self._lazy_markdown_cb = None
        self._page = _StubPage()

    def page(self):
        return self._page

    def _schedule_render(self, immediate=False):
        pass


def _make_card(monkeypatch):
    import app.widgets.message_card as mc
    from app.widgets.message_card import MessageCard

    # 固定编辑工具集合，避免依赖系统插件加载时序
    monkeypatch.setattr(mc, "_edit_tools", lambda: frozenset({"edit", "write", "multi_edit"}))

    _ensure_qapp()
    card = MessageCard(role="assistant")
    card._lazy_rendered = True
    card.viewer = _StubViewer()
    return card


# ─────────────────────────────────────────────
# 核心不变量：编辑工具完成后必须重设懒回调
# ─────────────────────────────────────────────


def test_edit_tool_result_resets_lazy_md_cb(monkeypatch):
    """编辑工具 append_tool_result 后 _lazy_markdown_cb 必须重设（非 None）。

    旧逻辑编辑分支跳过渲染时连 cb 一起跳过 → 停止时非流式渲染拿旧
    _markdown_text → save/restore 吞掉 DOM 完成框。
    """
    card = _make_card(monkeypatch)
    v = card.viewer
    assert v is not None
    tid = "call_edit_1"

    # 1) 工具参数流式 → 运行框注入
    card.update_tool_streaming(tool_call_id=tid, tool_name="edit", partial_args={"path": "a.py"})
    # 2) 参数收完 → 完成态预览框
    card.finish_tool_streaming(
        tool_call_id=tid, tool_name="edit", arguments={"path": "a.py", "oldString": "x", "newString": "y"}
    )
    # 3) 模拟"最后一次流式渲染已消费 cb"（6075/6141 行消费后状态）
    v._lazy_markdown_cb = None
    # 4) 工具结果返回（编辑分支：JS 注入 + 跳过渲染）
    card.append_tool_result(
        tool_name="edit",
        arguments={"path": "a.py", "oldString": "x", "newString": "y"},
        result="ok",
        success=True,
        tool_call_id=tid,
    )
    assert v._lazy_markdown_cb is not None, (
        "编辑工具 append_tool_result 后必须重设 _lazy_markdown_cb——否则停止时"
        "非流式渲染用旧 md，save/restore 吞掉 DOM 中的完成框"
    )


def test_edit_tool_lazy_md_regenerates_tool_block(monkeypatch):
    """重设的 cb 生成的 markdown 必须含工具块（停止渲染的 HTML 才会有完成框）。

    同时验证 _tool_md_cache 被 finish_streaming 清空后（6628 行），cb 仍能从
    _content_data 重新生成含工具块的 md（懒缓存回退路径 9918）。
    """
    card = _make_card(monkeypatch)
    v = card.viewer
    assert v is not None
    tid = "call_edit_2"

    card.update_tool_streaming(tool_call_id=tid, tool_name="edit", partial_args={"path": "b.py"})
    card.finish_tool_streaming(
        tool_call_id=tid, tool_name="edit", arguments={"path": "b.py", "oldString": "p", "newString": "q"}
    )
    v._lazy_markdown_cb = None
    card.append_tool_result(
        tool_name="edit",
        arguments={"path": "b.py", "oldString": "p", "newString": "q"},
        result="done",
        success=True,
        tool_call_id=tid,
    )
    assert v._lazy_markdown_cb is not None

    # 停止链路：finish_streaming 会清空 _tool_md_cache（6628），cb 必须仍可用
    v._tool_md_cache.clear()
    md = v._lazy_markdown_cb()
    assert "<tool>" in md, "重设 cb 生成的 md 应含工具块"
    assert tid in md, "md 应含 tool_call_id（供渲染层生成 data-tool-call-id 完成框）"


def test_edit_tool_lazy_md_renders_completed_block_html(monkeypatch):
    """端到端：cb 生成的 md 渲染出的 HTML 必须含完成态工具框（data-tool-call-id）。

    模拟停止时 _perform_update 非流式分支的关键路径：消费 cb → 渲染 →
    updateContent 重新生成完成框（替代被 save 移除且不 restore 的 DOM 块）。
    """
    from app.widgets.message_card import _render_markdown_to_html_cached_impl

    card = _make_card(monkeypatch)
    v = card.viewer
    assert v is not None
    tid = "call_edit_3"

    card.update_tool_streaming(tool_call_id=tid, tool_name="edit", partial_args={"path": "c.py"})
    card.finish_tool_streaming(
        tool_call_id=tid, tool_name="edit", arguments={"path": "c.py", "oldString": "m", "newString": "n"}
    )
    v._lazy_markdown_cb = None
    card.append_tool_result(
        tool_name="edit",
        arguments={"path": "c.py", "oldString": "m", "newString": "n"},
        result="ok",
        success=True,
        tool_call_id=tid,
    )
    assert v._lazy_markdown_cb is not None

    md = v._lazy_markdown_cb()
    html = _render_markdown_to_html_cached_impl(md, compact=True)
    assert f'data-tool-call-id="{tid}"' in html, "停止渲染的 HTML 应含编辑工具完成框（否则完成框被吞）"
    assert "cm-collapsible tool-block" in html


def test_non_edit_tool_result_keeps_render_schedule(monkeypatch):
    """非编辑工具行为不变：完成时仍触发 _schedule_render（immediate）+ cb 设置。"""
    card = _make_card(monkeypatch)
    v = card.viewer
    assert v is not None
    tid = "call_read_1"

    card.update_tool_streaming(tool_call_id=tid, tool_name="read", partial_args={"path": "d.py"})
    card.finish_tool_streaming(tool_call_id=tid, tool_name="read", arguments={"path": "d.py"})
    v._lazy_markdown_cb = None

    rendered = {"count": 0}

    def _fake_schedule(immediate=False):
        rendered["count"] += 1

    v._schedule_render = _fake_schedule
    card.append_tool_result(
        tool_name="read",
        arguments={"path": "d.py"},
        result="file content",
        success=True,
        tool_call_id=tid,
    )
    assert v._lazy_markdown_cb is not None, "非编辑工具也应设置懒回调"
    assert rendered["count"] >= 1, "非编辑工具完成应触发渲染调度"
