# -*- coding: utf-8 -*-
"""流式活动坞（Streaming Dock）测试：骨架资产 + Python 状态同步。

说明：测试环境无法创建 QWebEngineView（需 Qt.AA_ShareOpenGLContexts
在 QCoreApplication 创建前设置），因此骨架验证采用
"模块级资产常量 + inspect 校验骨架模板引用"的方式，
覆盖 (1) 资产内容正确 (2) 资产确实接入骨架模板 两条不变量。
"""

import inspect
import sys

from PyQt5.QtWidgets import QApplication

from app.widgets import message_card as mc
from app.widgets.message_card import CodeWebViewer, MessageCard


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


def test_streaming_dock_css_content():
    """坞态 CSS 必须包含：flex 调换、order 沉底、110px 限高。"""
    css = mc._STREAMING_DOCK_CSS
    assert "body.streaming-dock" in css
    assert "flex-direction: column" in css
    assert "body.streaming-dock #tool-section" in css
    assert "order: 2" in css
    assert "body.streaming-dock #tool-content" in css
    assert "max-height: 110px" in css


def test_streaming_dock_js_content():
    """坞态 JS 必须包含：_setStreamingDock 函数、流式标志、简洁模式守卫、滚动补偿。"""
    js = mc._STREAMING_DOCK_JS
    assert "function _setStreamingDock" in js
    assert "window._streamingActive" in js
    # 仅简洁模式启用坞态
    assert "_toolCompactMode" in js
    # 归位滚动补偿（防阅读位置跳动）
    assert "scrollTop" in js


def test_skeleton_template_includes_dock_assets():
    """骨架模板必须引用坞态资产常量（防止"定义了但没接进去"）。"""
    src = inspect.getsource(CodeWebViewer._load_skeleton)
    assert "_STREAMING_DOCK_CSS" in src
    assert "_STREAMING_DOCK_JS" in src


class _StubPage:
    def __init__(self):
        self.js_calls = []

    def runJavaScript(self, js_code):
        self.js_calls.append(js_code)


class _ViewerStub:
    """CodeWebViewer 桩：绑定真实方法，提供最小接口（无 WebEngine）。"""

    _sync_streaming_dock = CodeWebViewer._sync_streaming_dock
    finish_streaming = CodeWebViewer.finish_streaming
    _auto_collapse_tool_section = CodeWebViewer._auto_collapse_tool_section

    def __init__(self):
        self._is_js_ready = True
        self._page = _StubPage()
        self._streaming = True
        self.render_calls = 0
        # 与 CodeWebViewer._init_render_state 同语义：渲染序号，finish_streaming
        # 递增使在途线程池任务过期（9c76d04f 新增，stub 需同步）
        self._render_seq: int = 0

    def page(self):
        return self._page

    def _schedule_render(self, immediate=False):
        self.render_calls += 1


def test_sync_streaming_dock_injects_js():
    """_sync_streaming_dock 必须注入 _setStreamingDock(true/false)。"""
    stub = _ViewerStub()
    stub._sync_streaming_dock(True)
    assert "_setStreamingDock(true)" in stub._page.js_calls[-1]
    stub._sync_streaming_dock(False)
    assert "_setStreamingDock(false)" in stub._page.js_calls[-1]


def test_sync_streaming_dock_skips_when_js_not_ready():
    """JS 未就绪时不注入（_on_js_ready 会兜底同步）。"""
    stub = _ViewerStub()
    stub._is_js_ready = False
    stub._sync_streaming_dock(True)
    assert stub._page.js_calls == []


def test_finish_streaming_turns_dock_off():
    """finish_streaming 必须关闭坞态并触发最终渲染。"""
    stub = _ViewerStub()
    stub.finish_streaming()
    assert stub._streaming is False
    assert any("_setStreamingDock(false)" in js for js in stub._page.js_calls)
    assert stub.render_calls >= 1


class _StubViewerForCard:
    """MessageCard 用 viewer 桩（参照 test_message_card_tool_streaming 模式）。"""

    def __init__(self):
        self._streaming = False
        self.dock_calls = []

    def _sync_streaming_dock(self, active):
        self.dock_calls.append(active)


def test_start_streaming_anim_turns_dock_on():
    """MessageCard.start_streaming_anim 必须对 viewer 开启坞态。"""
    _ensure_qapp()
    card = MessageCard(role="assistant")
    card._lazy_rendered = True
    card.viewer = _StubViewerForCard()
    card.start_streaming_anim()
    assert card.viewer.dock_calls == [True]
