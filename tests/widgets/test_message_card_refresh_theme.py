# -*- coding: utf-8 -*-
"""回归测试：CodeWebViewer.refresh_theme 不得抛 NameError

2026-09-06 bug：图表主题运行时同步在 _chart_reset_js 中引用 body_font_size，
该名字只是 _refresh_viewer_font_css 的局部变量，refresh_theme 作用域内未定义
→ f-string 求值抛 NameError: name 'body_font_size' is not defined。

中断链：批处理主题刷新 → 每窗口 _apply_runtime_ui_settings → 消息卡
refresh_theme() 抛异常 → 该窗口后续刷新（输入框 refresh_style、设置弹窗
全部卡片）被 except 吞掉跳过 → 切深色后输入框/设置卡片样式停留旧主题。
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


class _StubPage:
    """记录 runJavaScript 调用，无 WebEngine"""

    def __init__(self):
        self.js_calls = []

    def runJavaScript(self, js_code, callback=None):
        self.js_calls.append(js_code)


def _make_viewer_stub():
    """__new__ 绕过 WebEngine 构造，绑定真实 refresh_theme + 最小属性"""
    from types import MethodType

    from app.widgets.message_card import CodeWebViewer

    v = CodeWebViewer.__new__(CodeWebViewer)
    v._last_theme_version = -1
    v._render_seq = 0
    v._stub_page = _StubPage()
    v.page = MethodType(lambda self: self._stub_page, v)
    v.isVisible = MethodType(lambda self: False, v)
    return v


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


def test_refresh_theme_no_name_error(qapp=None):
    """核心回归：refresh_theme 完整执行，不抛 NameError"""
    _ensure_qapp()
    from app.utils.theme_refresh import ThemeRefreshCoordinator

    ThemeRefreshCoordinator._current_theme_id = None  # 强制版本推进，绕过幂等
    v = _make_viewer_stub()
    v.refresh_theme()  # 修复前：NameError: body_font_size


def test_refresh_theme_injects_chart_theme_js():
    """图表主题 JS（_MMD_THEME_VARS / _applyChartTheme）必须被注入"""
    _ensure_qapp()
    from app.utils.theme_refresh import ThemeRefreshCoordinator

    ThemeRefreshCoordinator._current_theme_id = None
    v = _make_viewer_stub()
    v.refresh_theme()
    assert v._stub_page.js_calls, "refresh_theme 未注入任何 JS"
    chart_js = v._stub_page.js_calls[0]
    assert "_applyChartTheme" in chart_js
    assert "_MMD_THEME_VARS" in chart_js
    # 字号变量必须以具体数值注入（NaN/undefined/空 均为坏值）
    assert "fontSize: '" in chart_js and "px'" in chart_js
