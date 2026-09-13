# -*- coding: utf-8 -*-
"""运行框进度徽标测试：编辑类工具显示 +N/-M 取代字数，非编辑类保持字数。

⚠️ QApplication 必须先于 message_card 导入（qfluentwidgets 的 qconfig 需 QApplication 已存在，
否则构造 QWidget 抛 RuntimeError / 连锁 native 崩 0xC0000409）。
"""

import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
_APP = QApplication.instance() or QApplication(sys.argv)

from app.widgets import message_card as mc  # noqa: E402
from app.widgets.message_card import MessageCard, _render_tool_streaming_block  # noqa: E402


class _StubPage:
    def __init__(self):
        self.calls = []

    def runJavaScript(self, js, cb=None):
        self.calls.append(js)


class _StubViewer:
    def __init__(self):
        self._page = _StubPage()
        self._streaming = True
        self._tool_target_id = "tool-content"
        self._tool_compact_mode = True
        self._tool_dom_dirty = False
        self._tool_dom_dirty_gen = 0
        self._injected_pending_tools = set()

    def page(self):
        return self._page

    def _schedule_render(self, immediate=False):
        pass


def _make_card(monkeypatch):
    monkeypatch.setattr(mc, "_edit_tools", lambda: frozenset({"edit", "write", "multi_edit"}))
    card = MessageCard(role="assistant")
    card._lazy_rendered = True
    card.viewer = _StubViewer()
    return card


def test_badge_shows_line_stats_and_hides_chars():
    html = _render_tool_streaming_block(
        tool_call_id="t1",
        tool_name="edit",
        preview='编辑 "a.py" 中',
        char_count=0,
        completed=False,
        add_lines=8,
        del_lines=3,
    )
    assert "+8" in html and "-3" in html
    assert "字符" not in html


def test_badge_keeps_char_count_for_other_tools():
    html = _render_tool_streaming_block(
        tool_call_id="t2",
        tool_name="read",
        preview='读取 "a.py" 中',
        char_count=42,
        completed=False,
    )
    assert "(42字符)" in html


def test_badge_silent_when_completed():
    html = _render_tool_streaming_block(
        tool_call_id="t3",
        tool_name="edit",
        preview='编辑 "a.py"',
        char_count=99,
        completed=True,
        add_lines=8,
        del_lines=3,
    )
    assert "字符" not in html and "+8" not in html


def test_update_tool_streaming_passes_line_stats(monkeypatch):
    card = _make_card(monkeypatch)
    card.update_tool_streaming(
        tool_call_id="t4",
        tool_name="edit",
        partial_args={"_status": "loading", "_args_len": 120, "_path": "a.py", "_add_lines": 9, "_del_lines": 4},
    )
    js = card.viewer._page.calls[-1]
    assert "+9" in js and "-4" in js
    assert "字符" not in js


def test_update_tool_streaming_fallback_text_without_path(monkeypatch):
    """progress 阶段 path 还没到达：显示工具级文案，不再空窗「准备中...」"""
    card = _make_card(monkeypatch)
    card.update_tool_streaming(
        tool_call_id="t5", tool_name="edit", partial_args={"_status": "loading", "_args_len": 10}
    )
    js = card.viewer._page.calls[-1]
    assert "准备中" not in js
    assert "编辑文件" in js
