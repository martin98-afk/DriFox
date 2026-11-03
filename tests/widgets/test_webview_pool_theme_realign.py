# -*- coding: utf-8 -*-
"""回归测试：池化复用 viewer 的主题对齐 + 注入变量集完整性

2026-11-03 bug：欢迎卡片偶发「主题切换后 HTML 内部主题不刷新，重建几次才恢复」。

根因一（池化残留）：viewer 归还 WebViewPool 后挂在 WA_DontShowOnScreen 宿主上，
不在任何窗口 widget 树 → 主题刷新 findChildren 扫不到 → 池中页面停留旧主题
CSS 变量。跨主题切换后取用复用，body HTML 全 var() 引用按旧变量渲染 →
深色白字画在浅色卡片上。取用侧此前只对齐了简洁模式（sync_compact_mode_to_js）
与字体（_apply_font_vars），漏了主题。
    修复：取用侧补 pooled.refresh_theme()（版本幂等，主题没变零 IPC）。

根因二（注入集缺口）：_build_css_var_js 不含骨架 :root 的语义派生层变量
（--accent-text/--accent-soft/--accent-soft-strong/--accent-border-weak/
--accent-glow/--row-*），在线刷新的卡片这些颜色同样停留旧主题。
    修复：注入集补全，与 chart_viewer_card._inject_theme_vars 同源对齐。
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


def test_build_css_var_js_covers_skeleton_derived_vars():
    """注入 JS 必须覆盖骨架 :root 的语义派生层变量（8 个缺失项）

    骨架（card_viewers._load_skeleton）:root 用主题字面量声明这批变量，
    运行时注入通道若不覆盖，已存在卡片切换主题后这些颜色停留旧主题。
    """
    _ensure_qapp()
    from app.utils.design_tokens import current_theme
    from app.utils.theme_refresh import ThemeRefreshCoordinator, _build_css_var_js

    ThemeRefreshCoordinator._current_theme_id = None  # 使 get_or_build_js 缓存重建
    ThemeRefreshCoordinator.on_theme_changed("test-light")  # 推进版本，缓存随版本分桶
    js = _build_css_var_js(current_theme(), is_light=True)
    for var in (
        "--accent-text",
        "--accent-soft",
        "--accent-soft-strong",
        "--accent-border-weak",
        "--accent-glow",
        "--row-alt",
        "--row-hover",
        "--row-header",
    ):
        assert f"'{var}'" in js, f"注入 JS 缺失骨架变量 {var}"
    # 主题字面量必须真的进值（不能全空串：hover_bg/selected_bg 缺键时）
    assert "setProperty('--accent-soft','')" not in js
    assert "setProperty('--accent-soft-strong','')" not in js
    # 深浅分支：浅色 --row-header 用深色叠加
    assert "rgba(15, 23, 42, 0.06)" in js


def test_build_css_var_js_dark_branch():
    """深色分支：--row-* 用白色叠加，--accent-glow 用 rgba 派生"""
    _ensure_qapp()
    from app.utils.design_tokens import current_theme
    from app.utils.theme_refresh import ThemeRefreshCoordinator, _build_css_var_js

    ThemeRefreshCoordinator._current_theme_id = None
    ThemeRefreshCoordinator.on_theme_changed("test-dark")
    js = _build_css_var_js(current_theme(), is_light=False)
    assert "rgba(255, 255, 255, 0.05)" in js
    assert "setProperty('--accent-glow','rgba(" in js


def _make_viewer_stub():
    """__new__ 绕过 WebEngine 构造，绑定真实 refresh_theme + 最小属性。

    与 test_message_card_refresh_theme 同款桩模式。
    """
    from types import MethodType

    from app.widgets.message_card import CodeWebViewer

    v = CodeWebViewer.__new__(CodeWebViewer)
    v._last_theme_version = -1
    v._render_seq = 0
    v._final_render_seq = 0
    v._final_render_inflight = False
    v._has_charts = False

    class _StubPage:
        def __init__(self):
            self.js_calls = []

        def runJavaScript(self, js_code, callback=None):
            self.js_calls.append(js_code)

    v._stub_page = _StubPage()
    v.page = MethodType(lambda self: self._stub_page, v)
    v.isVisible = MethodType(lambda self: True, v)
    return v


def test_pooled_reuse_injects_theme_after_version_bump():
    """跨主题切换的池化复用必须重新注入 CSS 变量

    模拟池化复用链：viewer 上次注入于版本 v0（在树上正常刷新）→ 归还池 →
    主题切换推进版本 → 取用复用调 refresh_theme → 必须注入（版本幂等失效），
    且 _last_theme_version 更新。主题没变时（版本相同）refresh_theme 零 IPC，
    复用旧值——同样断言，防取用侧改成无条件注入后浪费 IPC。
    """
    _ensure_qapp()
    from app.utils.theme_refresh import ThemeRefreshCoordinator

    ThemeRefreshCoordinator.on_theme_changed("test-a")  # 真实主题切换路径：推进版本
    v = _make_viewer_stub()
    v.refresh_theme()
    v0 = ThemeRefreshCoordinator.get_version()
    assert v._last_theme_version == v0
    n_calls_after_first = len(v._stub_page.js_calls)
    assert n_calls_after_first > 0, "首次注入失败"

    # 同版本再次调用（模拟主题未变的池化复用）：幂等零 IPC
    v.refresh_theme()
    assert len(v._stub_page.js_calls) == n_calls_after_first

    # 版本推进（模拟池中跨越一次主题切换后再复用）：必须重新注入
    ThemeRefreshCoordinator.on_theme_changed("test-b")
    v.refresh_theme()
    assert len(v._stub_page.js_calls) > n_calls_after_first
    assert v._last_theme_version == ThemeRefreshCoordinator.get_version()


def test_pooled_acquire_path_calls_refresh_theme():
    """源码锚：池化取用分支必须接入主题对齐调用

    ensure_rendered 的 pooled 复用分支在字体对齐（_apply_font_vars）之后必须
    调 refresh_theme。锁链路，防止后续重构静默摘掉（字体对齐先例证明这类
    「池化残留」必须逐项显式对齐，靠全局刷新扫不到池中实例）。
    """
    import inspect

    from app.widgets.message_card import MessageCard

    src = inspect.getsource(MessageCard.ensure_rendered)
    pooled_block = src[src.index("pooled = WebViewPool.get_instance().acquire") :]
    pooled_block = pooled_block[: pooled_block.index("if self.viewer is None")]
    assert "pooled.refresh_theme()" in pooled_block, "池化取用分支缺主题对齐调用"
    assert "pooled._apply_font_vars()" in pooled_block
    # 对齐调用必须在 show() 之后（isVisible 语义依赖父链恢复）
    assert pooled_block.index("pooled.show()") < pooled_block.index("pooled.refresh_theme()")
