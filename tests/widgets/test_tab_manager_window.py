# -*- coding: utf-8 -*-
"""TabManagerWindow 组件测试"""

import pytest
from PyQt5.QtWidgets import QLabel

from app.widgets.tab_manager_window import TabManagerWindow, EmptyStateWidget


@pytest.fixture(autouse=True)
def reset_singleton():
    """每个测试前后重置单例、防重入守卫时间戳和 TrayManager 引用"""
    from app.tray_manager import TrayManager

    TabManagerWindow._instance = None
    TabManagerWindow._last_toggle_time = 0.0  # 防重入守卫时间戳（类变量，跨测试残留需重置）
    TrayManager.get_instance()._tab_manager_window = None
    yield
    TabManagerWindow._instance = None
    TabManagerWindow._last_toggle_time = 0.0
    TrayManager.get_instance()._tab_manager_window = None


class TestEmptyStateWidget:
    def test_create(self, qtbot):
        w = EmptyStateWidget()
        qtbot.addWidget(w)

    def test_shows_empty_text(self, qtbot):
        """空状态页显示提示文本（无新建按钮——入口在 Tab 面板）"""
        w = EmptyStateWidget()
        qtbot.addWidget(w)
        texts = [lbl.text() for lbl in w.findChildren(QLabel)]
        assert any("没有打开的窗口" in t for t in texts)


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

        # 禁用（重置防重入守卫时间戳，避免 1s 内重复调用被忽略）
        TabManagerWindow._last_toggle_time = 0.0
        TabManagerWindow.toggle_mode(enable=False)
        assert tray_manager._tab_manager_window is None

    def test_toggle_mode_idempotent(self, qtbot):
        """重复切换应幂等"""
        from app.tray_manager import TrayManager

        TabManagerWindow.toggle_mode(enable=True)
        tm1 = TabManagerWindow.get_instance()

        TabManagerWindow._last_toggle_time = 0.0
        TabManagerWindow.toggle_mode(enable=True)  # 再次启用
        tm2 = TabManagerWindow.get_instance()
        assert tm1 is tm2

        TabManagerWindow._last_toggle_time = 0.0
        TabManagerWindow.toggle_mode(enable=False)
        TabManagerWindow._last_toggle_time = 0.0
        TabManagerWindow.toggle_mode(enable=False)  # 再次禁用
        assert TrayManager.get_instance()._tab_manager_window is None

    def test_toggle_mode_releases_lock(self, qtbot):
        """切换完成后锁应释放"""
        TabManagerWindow.toggle_mode(enable=True)
        tm = TabManagerWindow.get_instance()
        assert tm._is_transitioning is False
