# -*- coding: utf-8 -*-
"""回归测试：输入框模型按钮服务商 icon 随主题切换 + 自定义服务商回退图标

用户实测两个缺陷（2026-11-04 截图）：
1. 输入框模型选择处的服务商 icon 不随系统主题切换 —— 深浅来回切后仍显示
   旧主题颜色的图标。
2. 自定义服务商（不在 ProviderRegistry 注册表）没有 icon —— 按钮处空白。

根因：
1. ``MainWidget._update_model_selector_btn`` 用 ``icon.pixmap(15,15)`` 把
   服务商 icon 渲染成静态位图塞进 QLabel；主题刷新链
   ``_apply_runtime_ui_settings`` 5b 块重设了 ``_model_btn_text`` 与
   ``_settings_btn_icon``，唯独漏了 ``_model_btn_icon``。
2. ``get_provider_icon`` 对注册表查不到的名字直接返回空 QIcon（连 qrc
   回退都没有；except 分支反而有 ``get_icon("大模型")`` 兜底），
   ``_update_model_selector_btn`` 对空 icon 走 ``clear()`` → 空白。

修复：抽 ``_refresh_provider_icon_pixmap``（空 icon 回退 qrc「大模型」），
模型切换与主题刷新 5b 块共用。
"""

import sys
import types

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QLabel


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return _ensure_qapp()


def _make_host(icon_provider: str = "zhipu"):
    """用 __new__ 绕过重量级 OpenAIChatToolWindow.__init__，只补 icon 刷新用到的状态"""
    from app.main_widget import OpenAIChatToolWindow

    host = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    host._current_provider_name = "cfg-1"
    host._valid_configs = {
        "cfg-1": {"provider_name": icon_provider, "display_name": "智谱"}
    }
    host._model_btn_icon = QLabel()
    host._model_btn_icon.setFixedSize(18, 18)
    host._model_btn_icon.setScaledContents(True)
    return host


def _set_stale_red_pixmap(label: QLabel) -> None:
    """塞一块纯红位图，模拟旧主题遗留的静态 pixmap"""
    stale = QPixmap(15, 15)
    stale.fill(QColor(255, 0, 0))
    label.setPixmap(stale)


class TestProviderIconPixmapRefresh:
    def test_refresh_replaces_stale_pixmap(self, monkeypatch):
        """重新调用必须替换掉旧 pixmap（静态位图不会自动更新的本质是没人重设）"""
        import app.main_widget as mw

        icon = QIcon("icons/大模型.svg")
        assert not icon.isNull(), "测试依赖的大模型 svg 加载失败"
        monkeypatch.setattr(mw, "get_provider_icon", lambda pname: icon)

        host = _make_host()
        _set_stale_red_pixmap(host._model_btn_icon)

        host._refresh_provider_icon_pixmap()

        pm = host._model_btn_icon.pixmap()
        assert pm is not None and not pm.isNull()
        img = pm.toImage()
        # 中心像素不再是纯红（旧位图已被替换）
        c = img.pixelColor(img.width() // 2, img.height() // 2)
        assert not (c.red() >= 250 and c.green() < 10 and c.blue() < 10)

    def test_provider_name_resolved_from_valid_configs(self, monkeypatch):
        """icon 按 provider_name 查（不按 UUID config_id 查）"""
        import app.main_widget as mw

        calls = []
        monkeypatch.setattr(
            mw, "get_provider_icon", lambda pname: calls.append(pname) or QIcon("icons/大模型.svg")
        )

        host = _make_host(icon_provider="zhipu")
        host._refresh_provider_icon_pixmap()

        assert calls == ["zhipu"], f"应按 provider_name 查 icon，实际传了 {calls}"

    def test_unknown_provider_falls_back_to_generic_icon(self, monkeypatch):
        """注册表查不到（自定义服务商）→ 回退「大模型」图标，绝不清空"""
        import app.main_widget as mw

        monkeypatch.setattr(mw, "get_provider_icon", lambda pname: QIcon())  # 空 icon

        host = _make_host(icon_provider="我的自定义服务商")
        host._model_btn_icon.clear()

        host._refresh_provider_icon_pixmap()

        pm = host._model_btn_icon.pixmap()
        assert pm is not None and not pm.isNull(), "自定义服务商 icon 应回退通用图标而非空白"

    def test_fallback_icon_follows_theme(self, monkeypatch):
        """回退图标走 get_icon（_ThemeIconEngine），深浅主题下像素颜色不同"""
        import app.main_widget as mw
        from app.utils.utils import _is_current_theme_light

        monkeypatch.setattr(mw, "get_provider_icon", lambda pname: QIcon())
        host = _make_host(icon_provider="我的自定义服务商")

        host._refresh_provider_icon_pixmap()
        img_light = host._model_btn_icon.pixmap().toImage()
        light_sum = _opaque_sum(img_light)

        # 翻转主题判定（_ThemeIconEngine 的 key 含主题标识 → 下次取 pixmap 重渲染）
        monkeypatch.setattr(
            "app.utils.utils._is_current_theme_light", lambda: not _is_current_theme_light()
        )
        host._refresh_provider_icon_pixmap()
        img_dark = host._model_btn_icon.pixmap().toImage()
        dark_sum = _opaque_sum(img_dark)

        assert light_sum is not None and dark_sum is not None
        assert light_sum != dark_sum, "回退图标未随主题变色"


def _opaque_sum(img) -> "int | None":
    """不透明像素 RGB 和（与 test_theme_refresh_mcp_and_mention_icon 同口径）"""
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() > 200:
                return c.red() + c.green() + c.blue()
    return None


class TestRefreshChainCoverage:
    def test_theme_refresh_block_calls_provider_icon_reset(self):
        """主题刷新 5b 块必须重设服务商 icon（缺陷 1 的守卫：漏调用即回归）"""
        import inspect

        from app.main_widget import OpenAIChatToolWindow

        body = inspect.getsource(OpenAIChatToolWindow._apply_runtime_ui_settings)
        assert "_refresh_provider_icon_pixmap" in body, (
            "_apply_runtime_ui_settings 未调用 _refresh_provider_icon_pixmap，"
            "主题切换后服务商 icon 会停留旧主题位图"
        )

    def test_update_model_selector_btn_never_clears_icon(self):
        """空 icon 必须走回退图标，禁止 clear()（缺陷 2 的守卫）"""
        import inspect

        from app.main_widget import OpenAIChatToolWindow

        body = inspect.getsource(OpenAIChatToolWindow._update_model_selector_btn)
        assert "_model_btn_icon.clear()" not in body
        assert "_refresh_provider_icon_pixmap()" in body
