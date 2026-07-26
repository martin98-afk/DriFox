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
