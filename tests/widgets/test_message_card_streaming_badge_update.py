# -*- coding: utf-8 -*-
"""回归测试：运行框流式徽标更新不得被「预览文本去重」误杀。

根因：
  `_inject_tool_streaming_html` 的内容去重原先只比较预览文本 `preview_content`。
  编辑类工具（write/edit/multi_edit）流式期间预览文本在路径完整后就恒定
  （worker 只提前提取 `_path`，content 不参与预览），于是此后每个进度事件都被
  判定为「无变化」直接 return —— `+N/-M` 行数与字符数徽标的更新被整体跳过，
  运行框视觉上"卡死"在首帧（如一直显示 `(32字符)`）。

修复：去重值改为 `(preview_content, badge_html)`，徽标变化必须重新注入。

验证方式：桩 viewer（真实 `_inject_tool_streaming_html` + 假 page 记录 JS），
直接驱动 `update_tool_streaming` 观察 JS 派发次数。
"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

# Qt 属性必须先于 message_card（顶层拉入 QWebEngineView）设置，否则 native crash
QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
# QApplication 也必须在导入 message_card 之前存在（qfluentwidgets 的 qconfig 在
# 无 QApplication 时创建会拿到无效 C++ 对象，后续构造 QWidget 直接 RuntimeError）
_APP = QApplication.instance() or QApplication(sys.argv)

from app.widgets import message_card as mc  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

TOOL_ID = "call_write_badge"


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


class _ViewerStub:
    """最小 viewer 桩：只保留 `_inject_tool_streaming_html` 依赖的字段。"""

    def __init__(self):
        self._page = _FakePage()
        self._tool_dom_dirty = False
        self._tool_dom_dirty_gen = 0
        self._injected_pending_tools = set()
        self._tool_target_id = "tool-content"

    def page(self):
        return self._page

    def _schedule_render(self, immediate=False):
        pass


def _make_card(monkeypatch):
    monkeypatch.setattr(mc, "_edit_tools", lambda: frozenset({"edit", "write", "multi_edit"}))
    card = MessageCard(role="assistant")
    card.viewer = _ViewerStub()
    card._content_data = []
    return card


def test_badge_change_triggers_injection_despite_same_preview(monkeypatch):
    """预览文本不变、只有徽标变化时必须重新注入（否则运行框停更在首帧）。"""
    card = _make_card(monkeypatch)
    page = card.viewer._page

    card.update_tool_streaming(TOOL_ID, "write", {"_status": "loading", "_args_len": 32})
    assert len(page.calls) == 1, "首帧应注入运行框"

    # 预览文本不变（无 _path → 兜底文案），仅字符数与行数增长
    card.update_tool_streaming(
        TOOL_ID, "write", {"_status": "loading", "_args_len": 5120, "_add_lines": 88, "_del_lines": 3}
    )
    assert len(page.calls) == 2, "徽标变化必须触发新注入"
    assert "+88" in page.calls[-1] and "-3" in page.calls[-1], "新注入应携带增删行数徽标"


def test_char_count_only_change_triggers_injection(monkeypatch):
    """非编辑类路径同理：字符数徽标变化不得被去重跳过。"""
    card = _make_card(monkeypatch)
    page = card.viewer._page

    card.update_tool_streaming(TOOL_ID, "read", {"_status": "loading", "_args_len": 128})
    n1 = len(page.calls)
    card.update_tool_streaming(TOOL_ID, "read", {"_status": "loading", "_args_len": 4096})
    assert len(page.calls) == n1 + 1, "字符数变化必须触发新注入"
    assert "4096" in page.calls[-1]


def test_identical_payload_still_deduplicated(monkeypatch):
    """预览与徽标都相同 → 继续去重（保证高频流式下的注入压力不变）。"""
    card = _make_card(monkeypatch)
    page = card.viewer._page

    payload = {"_status": "loading", "_args_len": 32}
    card.update_tool_streaming(TOOL_ID, "write", payload)
    n1 = len(page.calls)
    card.update_tool_streaming(TOOL_ID, "write", dict(payload))
    assert len(page.calls) == n1, "完全相同的更新应继续被去重"


def test_badge_inherits_completion_diff_colors(monkeypatch):
    """徽标必须用完成框同款内联色（不依赖 .tool-diff-stats__* 的 CSS 优先级）。"""
    card = _make_card(monkeypatch)
    page = card.viewer._page

    card.update_tool_streaming(
        TOOL_ID, "write", {"_status": "loading", "_args_len": 5120, "_add_lines": 88, "_del_lines": 3}
    )
    js = page.calls[-1]
    assert "#39d353" in js, "新增行数用完成框同款绿色"
    assert "#f85149" in js, "删除行数用完成框同款红色"


def test_preview_span_does_not_push_badge_to_right(monkeypatch):
    """预览 span 不得 flex-grow 撑满；否则徽标被顶到最右，而设计要与文字排在一起。"""
    card = _make_card(monkeypatch)
    page = card.viewer._page

    card.update_tool_streaming(TOOL_ID, "write", {"_status": "loading", "_args_len": 32})
    js = page.calls[-1]
    assert "flex: 0 1 auto" in js, "预览 span 应可收缩但不撑满，让徽标紧跟文字"
    assert "flex: 1 1 auto" not in js, "预览 span 不得撑满剩余空间"
