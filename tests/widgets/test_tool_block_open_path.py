# -*- coding: utf-8 -*-
"""工具完成框里的文件地址可点（data-open-path）回归。

背景：工具卡显示的路径长期是纯文本，要打开只能自己复制到资源管理器。
现在渲染层给路径打 data-open-path（显示文本可截断，属性存完整原文），
骨架点击委托在折叠头之前抢断，转 open_file 消息交宿主按会话 workdir 解析、
用系统程序打开。
"""

import re
import urllib.parse

from PyQt5.QtCore import QEventLoop, QObject, QTimer, pyqtSignal

from app.widgets.render_helpers import _linkify_path, render_tool_block

_LINK_RE = re.compile(r'<span class="tool-path-link" data-open-path="([^"]*)"')


def _head(html: str) -> str:
    """截到 body 之前的折叠头（body 里参数表格也含路径文本，避免误判）"""
    return html.split('class="cm-collapsible__body"')[0]


class TestFoldableHeaderPath:
    def test_desc_header_path_clickable_with_full_path(self):
        """有 description：右侧整段路径可点，属性存完整原文而非缩短版。"""
        long_path = "plugins/gateway-telegram/gateways/deep/nested/telegram_message_stream.py"
        html = render_tool_block(
            "edit",
            {"path": long_path, "description": "加编辑消息流式"},
            "已编辑",
            True,
            tool_call_id="tc1",
        )
        head = _head(html)
        m = _LINK_RE.search(head)
        assert m is not None
        assert m.group(1) == long_path
        assert "…" in head  # 显示仍是缩短版，只影响展示

    def test_no_desc_preview_path_clickable(self):
        """无 description：参数预览里的路径片段同样可点。"""
        head = _head(render_tool_block("edit", {"path": "a.py"}, "已编辑", True, tool_call_id="tc1"))
        assert 'data-open-path="a.py"' in head

    def test_no_path_param_no_link(self):
        """没有 path/file_path 的工具不产生可点元素。"""
        html = render_tool_block("bash", {"command": "ls -la"}, "ok", True, tool_call_id="tc9")
        assert "data-open-path" not in html


class TestInlinePreviewPath:
    def test_inline_preview_path_clickable(self, monkeypatch):
        """inline 卡（read/grep/list/glob）的预览里，路径片段可点。"""
        from app.tools.registry import ToolRegistry

        inst = ToolRegistry.get_instance()
        monkeypatch.setattr(inst, "get_render_mode", lambda _name: "inline")
        monkeypatch.setattr(
            inst,
            "get_preview",
            lambda _name: (lambda args: f"读取 {args.get('path', '')} 前 500 行"),
        )
        html = render_tool_block("read", {"path": "app/main.py"}, "x", True, tool_call_id="tc2")
        assert 'data-open-path="app/main.py"' in html
        assert "读取" in html


class TestLinkifySafety:
    def test_attr_and_text_escaped(self):
        """路径进 HTML 属性/文本前必须转义，不能被当成标签。"""
        html = _linkify_path("见 a&b<c.py 末尾", "a&b<c.py")
        assert 'data-open-path="a&amp;b&lt;c.py"' in html
        assert "<c.py" not in html

    def test_miss_returns_empty(self):
        """路径不在文本里：返回空串，调用方自行 escape 整段。"""
        assert _linkify_path("没有路径", "a.py") == ""


class TestSkeletonWiring:
    """骨架必须带点击委托，且抢断顺序在折叠头之前（否则点路径会顺带展开）。"""

    @staticmethod
    def _skeleton_html() -> str:
        from app.widgets.card_viewers import CodeWebViewer

        v = CodeWebViewer.__new__(CodeWebViewer)
        v._light_skeleton = True
        v._is_js_ready = False
        v._v2_last = None
        v._history_collapse_applied = False
        v._v2_live_key = ""
        v._v2_live_sent = ""
        v._v2_live_formalized_len = 0
        v._v2_live_formalize_ts = 0.0
        v._dock_sync_requested = False
        v._streaming = True
        captured = {}
        v.setHtml = lambda html, base=None: captured.setdefault("html", html)
        v.ensure_transparent_composition = lambda: None
        CodeWebViewer._load_skeleton(v)
        return captured.get("html", "")

    def test_delegate_precedes_collapsible_summary(self):
        html = self._skeleton_html()
        i_open = html.find("data-open-path")
        i_summary = html.find("closest('.cm-collapsible__summary')")
        assert i_open != -1, "骨架缺少 data-open-path 点击委托"
        assert i_summary != -1
        assert i_open < i_summary, "路径点击必须抢在折叠头之前处理"

    def test_path_link_style_present(self):
        assert ".tool-path-link" in self._skeleton_html()


class TestOpenFileMessage:
    """console 消息 open_file 解码 + 延迟派发（族⑤ 同链路）。"""

    class _PageStub(QObject):
        openFileRequested = pyqtSignal(str)

        def __init__(self):
            super().__init__()
            self.paths = []
            self.openFileRequested.connect(self.paths.append)

    @staticmethod
    def _pump_once():
        loop = QEventLoop()
        QTimer.singleShot(0, loop.quit)
        loop.exec_()

    def test_open_file_decoded_and_deferred(self, qapp):
        """encodeURIComponent 后的路径（盘符冒号/空格/中文）还原完整，且不在回调栈内 emit。"""
        from PyQt5.QtWebEngineWidgets import QWebEnginePage

        from app.widgets.card_viewers import ConsoleMonitorPage

        stub = self._PageStub()
        raw = "C:/工作 目录/app/main.py"
        msg = "pywebview_action:open_file:" + urllib.parse.quote(raw, safe="")
        ConsoleMonitorPage.javaScriptConsoleMessage(
            stub, QWebEnginePage.JavaScriptConsoleMessageLevel(0), msg, 1, "js"
        )
        assert stub.paths == [], "emit 仍发生在 Chromium 回调栈内（未延迟）"
        self._pump_once()
        assert stub.paths == [raw]
