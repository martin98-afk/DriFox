# -*- coding: utf-8 -*-
"""TabPanel 组件测试"""

from unittest.mock import MagicMock, patch

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QWidget

from app.widgets.tab_panel import TabItem, TabPanel, UIPluginRow


@pytest.fixture
def panel(qtbot):
    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    qtbot.addWidget(p)
    return p


class TestTabPanel:
    def test_gitee_account_row_exists(self, panel):
        """验证底部存在 GiteeAccountRow 且其右侧为设置按钮"""
        from app.widgets.cards.settings.gitee_card import GiteeAccountRow

        assert isinstance(panel._gitee_account_row, GiteeAccountRow)
        # 验证 GiteeAccountRow 内有设置按钮
        assert hasattr(panel._gitee_account_row, "_settings_btn")
        assert panel._gitee_account_row._settings_btn is not None

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


class TestUIPluginRowPositionMenu:
    """UIPluginRow 右键插入方位菜单"""

    def test_context_menu_policy(self, panel):
        """右键策略为 CustomContextMenu（右键不吞事件、走 customContextMenuRequested）"""
        row = UIPluginRow("测试插件", None, panel, plugin_name="test", card_id="card_x")
        assert row.contextMenuPolicy() == Qt.CustomContextMenu

    def test_position_menu_actions_and_signal(self, panel, qtbot):
        """菜单项为下/左/右/替换（无「上」，与 full 行为重复），带图标，触发后发射 positionRequested"""
        row = UIPluginRow("测试插件", None, panel, plugin_name="test", card_id="card_x")
        received = []
        row.positionRequested.connect(lambda cid, cont: received.append((cid, cont)))

        menu = row._build_position_menu()
        actions = menu.actions()
        labels = [a.text() for a in actions]
        assert labels == ["下", "左", "右", "替换"]
        # 每项都带图标（黑=显示位置，白=空白）
        assert all(not a.icon().isNull() for a in actions)
        # 触发「替换」→ full
        actions[3].trigger()
        assert received == [("card_x", "full")]
        # 触发「左」→ left
        actions[1].trigger()
        assert received == [("card_x", "full"), ("card_x", "left")]

    def test_refresh_ui_plugins_position_requested_connected(self, panel, qtbot):
        """refresh_ui_plugins 后系统/自定义插件行的 positionRequested 均已连接处理函数"""
        cards = {
            "sys_card": MagicMock(title="系统卡", plugin_name="sys_plugin"),
            "custom_card": MagicMock(title="自定义卡", plugin_name="custom_plugin"),
        }
        sys_plugin = MagicMock(is_system=True)
        custom_plugin = MagicMock(is_system=False)
        fake_pm = MagicMock()
        fake_pm.get_plugin.side_effect = lambda name: sys_plugin if name == "sys_plugin" else custom_plugin
        fake_registry = MagicMock()
        fake_registry.get_floating_cards.return_value = cards

        cm = MagicMock()
        cm.is_card_visible.return_value = True
        fake_win = MagicMock()

        class FakeHost(QWidget):
            _card_manager = cm
            _window_id = "w1"

            def get_current_window(self):
                return fake_win

        host = FakeHost()
        panel._test_host = host  # 强引用，避免测试结束被 GC 连带销毁 panel
        panel.setParent(host)

        with (
            patch("app.core.ui_plugin_registry.UIPluginRegistry.get_instance", return_value=fake_registry),
            patch("app.core.plugin_manager.PluginManager.get_instance", return_value=fake_pm),
        ):
            panel.refresh_ui_plugins()
            # 真实 emit 信号 → 已连接的 handler → move_floating_card 被调用
            panel._system_plugin_buttons[0].positionRequested.emit("sys_card", "top")
            panel._custom_plugin_buttons[0].positionRequested.emit("custom_card", "right")

        assert len(panel._system_plugin_buttons) == 1
        assert len(panel._custom_plugin_buttons) == 1

        assert fake_registry.move_floating_card.call_args_list == [
            (("sys_card", "top"), {"main_widget": fake_win}),
            (("custom_card", "right"), {"main_widget": fake_win}),
        ]

    def test_position_requested_moves_and_shows_hidden_card(self, panel, qtbot):
        """选位后调用 move_floating_card；卡片原本隐藏时再 toggle 确保显示"""
        cm = MagicMock()
        cm.is_card_visible.return_value = False
        fake_win = MagicMock()
        registry = MagicMock()
        registry.move_floating_card.return_value = True

        class FakeHost(QWidget):
            _card_manager = cm
            _window_id = "w1"

            def get_current_window(self):
                return fake_win

        host = FakeHost()
        panel._test_host = host
        panel.setParent(host)

        with patch("app.core.ui_plugin_registry.UIPluginRegistry.get_instance", return_value=registry):
            panel._on_ui_plugin_position_requested("card_1", "right")

        registry.move_floating_card.assert_called_once_with("card_1", "right", main_widget=fake_win)
        registry.toggle_floating_card.assert_called_once_with("card_1", main_widget=fake_win)

    def test_position_requested_visible_card_no_toggle(self, panel, qtbot):
        """卡片原本可见时只 move，不重复 toggle（避免隐藏已显示的卡片）"""
        cm = MagicMock()
        cm.is_card_visible.return_value = True
        fake_win = MagicMock()
        registry = MagicMock()
        registry.move_floating_card.return_value = True

        class FakeHost(QWidget):
            _card_manager = cm
            _window_id = "w1"

            def get_current_window(self):
                return fake_win

        host = FakeHost()
        panel._test_host = host
        panel.setParent(host)

        with patch("app.core.ui_plugin_registry.UIPluginRegistry.get_instance", return_value=registry):
            panel._on_ui_plugin_position_requested("card_1", "full")

        registry.move_floating_card.assert_called_once_with("card_1", "full", main_widget=fake_win)
        registry.toggle_floating_card.assert_not_called()

    def test_position_requested_move_failed_no_toggle(self, panel, qtbot):
        """move_floating_card 失败（未注册/非法方位）时不继续 toggle"""
        cm = MagicMock()
        cm.is_card_visible.return_value = False
        fake_win = MagicMock()
        registry = MagicMock()
        registry.move_floating_card.return_value = False

        class FakeHost(QWidget):
            _card_manager = cm
            _window_id = "w1"

            def get_current_window(self):
                return fake_win

        host = FakeHost()
        panel._test_host = host
        panel.setParent(host)

        with patch("app.core.ui_plugin_registry.UIPluginRegistry.get_instance", return_value=registry):
            panel._on_ui_plugin_position_requested("card_1", "invalid")

        registry.move_floating_card.assert_called_once_with("card_1", "invalid", main_widget=fake_win)
        registry.toggle_floating_card.assert_not_called()
