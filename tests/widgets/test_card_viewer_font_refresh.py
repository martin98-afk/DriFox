# -*- coding: utf-8 -*-
"""回归测试：全局字体切换必须让已存在的卡片跟随（字体走 CSS 变量注入）

2026-11-01 bug：切「全局字体」后，消息卡正文与欢迎卡内容停在旧字体，要重建
几次会话才刷出来。根因链：
1. 正文字体写在 QWebEngine 骨架的 <style> 里（``{self._viewer_font_css}``
   与十余处 ``font-family: '{font_family}'``）。
2. ``_refresh_viewer_font`` 只重渲 ``#content-placeholder``，换不掉 <style>。
3. 骨架 HTML 按字体分桶缓存，且 ``reset_for_reuse`` 刻意保留骨架（省掉
   文档重建）。池化复用的 viewer 永远带着旧字体骨架 → 只有等新建实例
   （``_load_skeleton``）才生效。

修法：骨架里的字体声明全部改读 :root 变量（``--dfx-font-family`` /
``--dfx-font-mono`` / ``--dfx-font-size``），新增 ``CodeWebViewer.refresh_font``
运行时注入覆盖，与主题 CSS 变量同构。
"""

import subprocess
import sys

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
try:
    from PySide6.QtWebEngineWidgets import (  # noqa: F401
        QWebEnginePage,
        QWebEngineSettings,
        QWebEngineView,
    )
except Exception:
    pass


class _StubPage:
    """记录 runJavaScript 调用，无 WebEngine"""

    def __init__(self):
        self.js_calls = []

    def runJavaScript(self, js_code, callback=None):
        self.js_calls.append(js_code)


def _make_viewer_stub(visible: bool = True):
    """__new__ 绕过 WebEngine 构造，绑定真实 refresh_font + 最小属性"""
    from types import MethodType

    from app.widgets.message_card import CodeWebViewer

    v = CodeWebViewer.__new__(CodeWebViewer)
    v._is_js_ready = True
    v._viewer_font_family = "旧字体"
    v._stub_page = _StubPage()
    v.page = MethodType(lambda self: self._stub_page, v)
    v.isVisible = MethodType(lambda self: visible, v)
    # refresh_font 末尾会重渲（_schedule_render）；桩里只记录不执行
    v._scheduled = []
    v._schedule_render = MethodType(lambda self, immediate=False: self._scheduled.append(immediate), v)
    return v


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


def test_font_css_js_is_valid_js():
    """生成的注入串必须是可编译的 JS

    🐛 QWebEngine 对 runJavaScript 里的语法错误**静默失败**（无回调、无日志、
    DOM 不变），Python 侧看起来全成功。字体名可能含引号/反斜杠，拼串出错
    就只能靠这条测试拦住。
    """
    _ensure_qapp()
    v = _make_viewer_stub()
    js = v._font_css_js()
    for var in ("--dfx-font-family", "--dfx-font-mono", "--dfx-font-size"):
        assert var in js, f"注入串缺少 {var}"
    # 用 node 编译一次（项目纪律：拼接 JS 的改动必须验证真实串可编译）
    try:
        proc = subprocess.run(
            ["node", "-e", "new Function(process.argv[1])", js],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except FileNotFoundError:
        pytest.skip("node 不可用，跳过 JS 编译校验")
    assert proc.returncode == 0, f"注入串无法编译: {proc.stderr}"


def test_refresh_font_injects_css_vars():
    """refresh_font 必须注入字体变量（此前只重渲正文，字体永不生效）"""
    _ensure_qapp()
    v = _make_viewer_stub(visible=True)
    v.refresh_font()
    assert v._stub_page.js_calls, "refresh_font 未注入任何 JS"
    joined = "".join(v._stub_page.js_calls)
    assert "--dfx-font-family" in joined


def test_refresh_font_defers_when_hidden():
    """隐藏卡不立即注入（省 IPC），置 pending 由 showEvent 补注入"""
    _ensure_qapp()
    v = _make_viewer_stub(visible=False)
    v.refresh_font()
    assert v._stub_page.js_calls == [], "隐藏卡不应立即发 IPC"
    assert v._font_css_pending is True

    # 恢复可见：showEvent 路径补注入 —— 这里直接验证 _apply_font_vars 可清掩
    v.isVisible = lambda: True
    v._apply_font_vars()
    assert v._stub_page.js_calls, "恢复可见后未补注入"
    assert v._font_css_pending is False


def test_refresh_font_invalidates_html_cache():
    """字体变化必须让实例级 markdown HTML 缓存失效

    工具块 <pre> 里内联了字体（render_helpers._render_text_output），缓存命中
    会让工具输出停在旧字体。
    """
    _ensure_qapp()
    v = _make_viewer_stub(visible=True)
    v._cached_streaming_html = "<p>old</p>"
    v._processed_md_hash = 123
    v._cached_raw_md_hash = 456
    v._needs_full_render = False
    v.refresh_font()
    assert v._cached_streaming_html is None
    assert v._processed_md_hash == 0
    assert v._needs_full_render is True
    assert v._scheduled == [True], "应触发一次同步全量重渲"


def test_skeleton_uses_font_vars():
    """骨架 HTML 的字体声明必须读 CSS 变量（否则 refresh_font 注入无效）"""
    import inspect

    from app.widgets.card_viewers import CodeWebViewer

    src = inspect.getsource(CodeWebViewer._load_skeleton)
    # :root 定义存在（fallback 值供未注入时使用）
    assert "--dfx-font-family:" in src
    assert "--dfx-font-mono:" in src
    # 字体规则改读变量：正文族 ≥6 处（table/think/v2-live/tool-summary 等），
    # 等宽族 ≥8 处（inline code / 代码块 / 行号 / diff 等）。直接写死字体值的
    # 声明换不掉 → 切字体后那些节点停旧字体。
    assert src.count("var(--dfx-font-family") >= 6, "正文字体规则未全部改读变量"
    assert src.count("var(--dfx-font-mono") >= 8, "等宽字体规则未全部改读变量"
    assert "font-family: '{font_family}', sans-serif;" in src  # 仅 :root 定义处
    assert src.count("font-family: '{font_family}', sans-serif;") == 1
