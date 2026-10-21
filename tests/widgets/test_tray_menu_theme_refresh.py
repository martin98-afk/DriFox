# -*- coding: utf-8 -*-
"""托盘右键菜单 主题/字体切换刷新 回归测试。

背景：托盘菜单只构建一次、不重建（见 tray_menu.py 模块注释），且不在任何
窗口树内 —— DriFox 的字体刷新链（apply_font_size_to_widget 走 findChildren）
够不到它，导致改字体后菜单文字不跟随（字族/字号都停在构建时的值）。

修复：菜单自己挂 ``aboutToShow`` 自检 —— 字体签名变了才重设 view 字体 QSS
并重算条目宽度（幂等，未变零开销）。

打桩方式：直接设置 ``design_tokens`` 的字体缓存变量（``_menu_font_sig``
的读取源），不触碰 Settings 单例 —— 后者在 Python 3.14 + PyQt5 下有
QConfig C++ 生命周期问题（同模式的 test_input_area_font_style_refresh.py
在本机 native crash）。

验证：
1. 字族变化 → 弹出前 view 字体 QSS 跟随新配置
2. 字号变化 → 条目宽度重算
3. 配置未变 → 不重复重建（幂等）
4. 主题切换后的菜单 QSS 仍由框架补刷链保证（冒烟防回归）
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import app.utils.design_tokens as design_tokens
from PyQt5.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _set_font(monkeypatch, family="微软雅黑", delta=0):
    """打桩字体配置：直接改 design_tokens 缓存（_get_global_font / scale_font_size 的读取源）"""
    monkeypatch.setattr(design_tokens, "_cached_font_family", family)
    monkeypatch.setattr(design_tokens, "_cached_font_size_key", "stub")
    monkeypatch.setattr(design_tokens, "_cached_font_size_delta", delta)
    monkeypatch.setattr(design_tokens, "_cached_font_size_base", 14)


def _make_menu():
    from qfluentwidgets import FluentIcon

    from app.widgets.modules.tray_menu import TrayContextMenu

    menu = TrayContextMenu()
    menu.add_hint_action(FluentIcon.UPDATE, "检查更新", "v0.6.1", lambda: None)
    return menu


def test_font_family_change_updates_view_font(monkeypatch, qapp):
    """字族变化 → 弹出菜单前刷新 view 字体 QSS"""
    _set_font(monkeypatch, family="微软雅黑")
    menu = _make_menu()
    menu.aboutToShow.emit()  # 首次弹出：应用当前配置
    assert "微软雅黑" in menu.view.styleSheet()

    # 运行期改字体（模拟设置卡切换）
    _set_font(monkeypatch, family="楷体")
    menu.aboutToShow.emit()

    assert "楷体" in menu.view.styleSheet()
    assert "微软雅黑" not in menu.view.styleSheet()
    assert menu.view.font().families() == ["楷体"]


def test_font_size_change_updates_item_widgets(monkeypatch, qapp):
    """字号变化 → 条目宽度随字体度量重算"""
    _set_font(monkeypatch, family="微软雅黑", delta=0)
    menu = _make_menu()
    menu.aboutToShow.emit()
    item = menu.menuActions()[0].property("item")
    before_w = item.sizeHint().width()

    _set_font(monkeypatch, family="微软雅黑", delta=10)
    menu.aboutToShow.emit()

    assert item.sizeHint().width() > before_w


def test_font_size_shrink_reduces_menu_width(monkeypatch, qapp):
    """字号调小 → 菜单宽度跟随收缩（QListWidget.adjustSize 只增不减的坑）"""
    _set_font(monkeypatch, family="微软雅黑", delta=10)
    menu = _make_menu()
    menu.aboutToShow.emit()
    big_w = menu.width()

    _set_font(monkeypatch, family="微软雅黑", delta=0)
    menu.aboutToShow.emit()
    assert menu.width() < big_w


def test_no_change_skips_recompute(monkeypatch, qapp):
    """配置未变 → 不重复重建（幂等，避免每次弹出付全量重算成本）"""
    _set_font(monkeypatch, family="微软雅黑")
    menu = _make_menu()
    menu.aboutToShow.emit()
    first_qss = menu.view.styleSheet()

    calls = []
    orig = menu._apply_menu_font

    def spy(sig):
        calls.append(1)
        return orig(sig)

    menu._apply_menu_font = spy
    menu.aboutToShow.emit()
    assert calls == []  # 未变则不再调用
    assert menu.view.styleSheet() == first_qss


def test_theme_switch_keeps_working(monkeypatch, qapp):
    """主题切换冒烟：菜单 QSS 深浅切换仍由框架补刷链保证（不回归）"""
    from qfluentwidgets import Theme, setTheme

    _set_font(monkeypatch, family="微软雅黑")
    menu = _make_menu()
    setTheme(Theme.DARK, lazy=True)
    menu.show()
    qapp.processEvents()

    qss = menu.styleSheet()
    assert "rgb(43, 43, 43)" in qss or "rgba(255, 255, 255, 0.08)" in qss
    menu.hide()
    qapp.processEvents()
