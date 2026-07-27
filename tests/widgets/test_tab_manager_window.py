# -*- coding: utf-8 -*-
"""TabManagerWindow 组件测试"""

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QPushButton

from app.widgets.tab_manager_window import TabManagerWindow, EmptyStateWidget


@pytest.fixture(autouse=True)
def reset_singleton():
    """每个测试前后重置单例和 TrayManager 引用"""
    from app.tray_manager import TrayManager

    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    yield
    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None


class TestEmptyStateWidget:
    def test_create(self, qtbot):
        w = EmptyStateWidget()
        qtbot.addWidget(w)

    def test_new_tab_button_exists(self, qtbot):
        w = EmptyStateWidget()
        qtbot.addWidget(w)
        btn = w.findChild(QPushButton)
        assert btn is not None
        assert "新建" in btn.text()

    def test_new_tab_button_click_emits_signal(self, qtbot):
        w = EmptyStateWidget()
        qtbot.addWidget(w)
        btn = w.findChild(QPushButton)

        with qtbot.waitSignal(w.newTabRequested, timeout=500):
            btn.click()


class TestTabManagerWindow:
    def test_singleton_create(self, qtbot):
        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        assert TabManagerWindow.get_instance() is tm

    def test_singleton_raises_on_second_init(self):
        TabManagerWindow._instance = None
        tm1 = TabManagerWindow.create_instance()
        assert tm1 is not None
        with pytest.raises(RuntimeError, match="单例"):
            TabManagerWindow()

    def test_initial_state(self, qtbot):
        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        assert tm.window_count == 0
        assert tm.get_current_window() is None
        # 初始应显示空状态页
        assert tm._content_area.currentWidget() is tm._empty_state

    def test_has_tab_panel(self, qtbot):
        tm = TabManagerWindow.create_instance()
        qtbot.addWidget(tm)
        assert tm._tab_panel is not None
        assert tm._content_area is not None

    def test_toggle_mode_enable_disable(self, qtbot):
        """启用再禁用 Tab 模式（无窗口时）"""
        from app.tray_manager import TrayManager

        # 初始状态：独立模式
        tray_manager = TrayManager.get_instance()
        assert tray_manager._tab_manager_window is None

        # 启用
        TabManagerWindow.toggle_mode(enable=True)
        assert TabManagerWindow.get_instance() is not None
        assert tray_manager._tab_manager_window is not None

        # 禁用
        TabManagerWindow.toggle_mode(enable=False)
        assert tray_manager._tab_manager_window is None

    def test_toggle_mode_idempotent(self, qtbot):
        """重复切换应幂等"""
        from app.tray_manager import TrayManager

        TabManagerWindow.toggle_mode(enable=True)
        tm1 = TabManagerWindow.get_instance()

        TabManagerWindow.toggle_mode(enable=True)  # 再次启用
        tm2 = TabManagerWindow.get_instance()
        assert tm1 is tm2

        TabManagerWindow.toggle_mode(enable=False)
        TabManagerWindow.toggle_mode(enable=False)  # 再次禁用
        assert TrayManager.get_instance()._tab_manager_window is None

    def test_toggle_mode_releases_lock(self, qtbot):
        """切换完成后锁应释放"""
        TabManagerWindow.toggle_mode(enable=True)
        tm = TabManagerWindow.get_instance()
        assert tm._is_transitioning is False

    def test_minimize_macos_uses_hide(self, _qt_app, monkeypatch):
        """macOS 上 Frameless 窗口的 showMinimized() 无效，必须走 hide()

        回归保护：之前直接连 showMinimized()，导致 macOS 用户点击
        TabManagerWindow 最小化按钮没反应。修复后应按平台分流。
        """
        import app.widgets.tab_manager_window as tm_module

        monkeypatch.setattr(tm_module.platform, "system", lambda: "Darwin")
        # 直接构造避开 toggle_mode 防重入守卫
        tm = TabManagerWindow.create_instance()
        from app.tray_manager import TrayManager

        TrayManager.get_instance()._tab_manager_window = tm

        called = {"hide": 0, "showMinimized": 0}
        monkeypatch.setattr(tm, "hide", lambda *a, **kw: called.__setitem__("hide", called["hide"] + 1))
        monkeypatch.setattr(
            tm, "showMinimized", lambda *a, **kw: called.__setitem__("showMinimized", called["showMinimized"] + 1)
        )

        tm._on_minimize_clicked()
        assert called["hide"] == 1
        assert called["showMinimized"] == 0

    def test_minimize_windows_uses_showminimized(self, _qt_app, monkeypatch):
        """Windows/Linux 走原生 showMinimized() 保留标准最小化行为"""
        import app.widgets.tab_manager_window as tm_module

        monkeypatch.setattr(tm_module.platform, "system", lambda: "Windows")
        tm = TabManagerWindow.create_instance()
        from app.tray_manager import TrayManager

        TrayManager.get_instance()._tab_manager_window = tm

        called = {"hide": 0, "showMinimized": 0}
        monkeypatch.setattr(tm, "hide", lambda *a, **kw: called.__setitem__("hide", called["hide"] + 1))
        monkeypatch.setattr(
            tm, "showMinimized", lambda *a, **kw: called.__setitem__("showMinimized", called["showMinimized"] + 1)
        )

        tm._on_minimize_clicked()
        assert called["hide"] == 0
        assert called["showMinimized"] == 1

    def test_minimize_button_emits_signal(self, _qt_app):
        """最小化按钮点击必须触发 minimizeRequested 信号（接线完整性）

        与 _on_minimize_clicked 的连线在 _setup_ui（line 630）。
        """
        tm = TabManagerWindow.create_instance()
        from app.tray_manager import TrayManager

        TrayManager.get_instance()._tab_manager_window = tm

        title_bar = tm._title_bar
        from PyQt5.QtTest import QSignalSpy

        spy = QSignalSpy(title_bar.minimizeRequested)
        title_bar._min_btn.click()
        assert len(spy) == 1, "最小化按钮点击应触发 minimizeRequested 信号"
