# -*- coding: utf-8 -*-
"""TabPanel 组件测试"""

import pytest
from PyQt5.QtCore import Qt

from app.widgets.tab_panel import TabItem, TabPanel


@pytest.fixture
def panel(qtbot):
    p = TabPanel()
    qtbot.addWidget(p)
    return p


class TestTabPanel:
    def test_initial_state(self, panel):
        assert panel.count == 0
        assert panel.active_index == -1

    def test_add_tab(self, panel):
        idx = panel.add_tab("测试会话")
        assert panel.count == 1
        assert idx == 0
        assert panel.active_index == 0  # 自动选中第一个

    def test_add_multiple_tabs(self, panel):
        idx1 = panel.add_tab("会话A")
        idx2 = panel.add_tab("会话B")
        assert panel.count == 2
        assert idx1 == 0
        assert idx2 == 1

    def test_remove_tab(self, panel):
        panel.add_tab("会话A")
        panel.add_tab("会话B")
        panel.remove_tab(0)
        assert panel.count == 1

    def test_remove_last_tab(self, panel):
        panel.add_tab("会话A")
        panel.remove_tab(0)
        assert panel.count == 0
        assert panel.active_index == -1

    def test_set_active_index(self, panel):
        panel.add_tab("会话A")
        panel.add_tab("会话B")
        panel.set_active_index(1)
        assert panel.active_index == 1
        assert panel._items[1]._selected is True
        assert panel._items[0]._selected is False

    def test_remove_active_tab_switches_to_adjacent(self, panel):
        panel.add_tab("会话A")
        panel.add_tab("会话B")
        panel.set_active_index(0)
        panel.remove_tab(0)  # 移除当前选中的
        assert panel.active_index == 0  # 切换到剩下的第一个

    def test_update_tab_title(self, panel):
        panel.add_tab("旧标题")
        panel.update_tab_title(0, "新标题")
        assert panel._items[0]._title == "新标题"

    def test_tab_selected_signal(self, panel, qtbot):
        """验证选中 Tab 时发射信号"""
        with qtbot.waitSignal(panel.tabSelected, timeout=1000) as blocker:
            panel.add_tab("会话A")

        assert blocker.args == [0]

    def test_tab_close_requested_signal(self, panel, qtbot):
        """验证关闭 Tab 时发射信号（通过 closeRequested 间接触发）"""
        panel.add_tab("会话A")
        panel.add_tab("会话B")

        with qtbot.waitSignal(panel.tabCloseRequested, timeout=1000) as blocker:
            # 触发 item 的 closeRequested
            panel._items[1].closeRequested.emit()

        assert blocker.args == [1]
