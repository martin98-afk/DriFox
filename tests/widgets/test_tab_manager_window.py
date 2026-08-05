# -*- coding: utf-8 -*-
"""TabManagerWindow 组件测试"""

import pytest
from PyQt5.QtWidgets import QLabel

from app.widgets.tab_manager_window import TabManagerWindow, EmptyStateWidget


@pytest.fixture(autouse=True)
def reset_singleton():
    """每个测试前后重置单例与 TrayManager 引用"""
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
