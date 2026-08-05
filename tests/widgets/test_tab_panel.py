# -*- coding: utf-8 -*-
"""TabPanel 组件测试"""

from unittest.mock import MagicMock, patch

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QFrame, QWidget

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

    def test_batch_add_rebuilds_once(self, panel):
        """批量添加 N 个 tab 时 _rebuild_team_layout 只调用 1 次（C1 批量布局）"""
        panel.begin_batch_add()
        with patch.object(panel, "_rebuild_team_layout", wraps=panel._rebuild_team_layout) as m:
            for i in range(5):
                panel.add_tab(f"批量tab-{i}")
            # 批量期间不触发重建
            assert m.call_count == 0
            panel.end_batch_add()
            # 结束后统一重建一次
            assert m.call_count == 1
        # 布局结果正确：5 个 tab 全部入列表布局
        assert panel.count == 5
        layout_widgets = [panel._list_layout.itemAt(i).widget() for i in range(panel._list_layout.count())]
        known = [w for w in layout_widgets if w is not None]
        assert len(known) == 5

    def test_batch_add_nested_balance(self, panel):
        """嵌套 begin/end 成对时仅最外层结束统一重建一次（C1）"""
        panel.begin_batch_add()
        panel.begin_batch_add()
        with patch.object(panel, "_rebuild_team_layout", wraps=panel._rebuild_team_layout) as m:
            panel.add_tab("tab-A")
            panel.end_batch_add()  # 内层结束：深度仍为 1，不重建
            assert m.call_count == 0
            panel.add_tab("tab-B")
            panel.end_batch_add()  # 外层结束：深度归零，重建一次
            assert m.call_count == 1
        assert panel.count == 2

    def test_batch_add_then_team_group_works(self, panel):
        """批量添加后团队归属仍可正常分组（C1 不破坏团队布局）"""
        panel.begin_batch_add()
        for i in range(3):
            panel.add_tab(f"tab-{i}")
        panel.end_batch_add()
        panel.set_tab_team(0, "team-A")
        panel.set_tab_team(1, "team-A")
        grp = panel._team_groups.get("team-A")
        assert grp is not None
        # 团队容器已进入列表布局
        layout_widgets = [panel._list_layout.itemAt(i).widget() for i in range(panel._list_layout.count())]
        assert grp in [w for w in layout_widgets if w is not None]

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


def _stretch_count(layout) -> int:
    """统计布局中 stretch（QSpacerItem，widget() is None）数量"""
    return sum(1 for i in range(layout.count()) if layout.itemAt(i) is not None and layout.itemAt(i).widget() is None)


def _outer_widgets(layout):
    """布局顶层所有 widget（不含 stretch）"""
    return [
        layout.itemAt(i).widget()
        for i in range(layout.count())
        if layout.itemAt(i) is not None and layout.itemAt(i).widget() is not None
    ]


class TestTeamGroupLayout:
    """团队分组布局完整性（回归：stretch 丢失 + 同一 widget 重复入布局 + 团队框置顶）"""

    def test_team_group_layout_single_stretch_no_duplicate(self, panel):
        """团队含 ≥2 成员 tab + 独立 tab：恰 1 个 stretch、无重复 widget、团队框置顶"""
        panel.add_tab("独立A")
        panel.add_tab("团队-1")
        panel.add_tab("团队-2")
        panel.set_tab_team(1, "teamA")
        panel.set_tab_team(2, "teamA")
        # 再新增独立 tab（历史 bug：扁平索引 insertWidget 越界插到 stretch 后）
        panel.add_tab("独立B")

        # 恰有 1 个 stretch（未丢失、未重复）
        assert _stretch_count(panel._list_layout) == 1

        # 顶层无重复 widget（历史 bug：同一 widget 被 addWidget + addItem 两次）
        outer = _outer_widgets(panel._list_layout)
        assert len(outer) == len({id(w) for w in outer}), f"顶层存在重复 widget: {outer}"

        # 团队框置顶、独立 tab 在下、stretch 最末
        # 顶层顺序：[QFrame(teamGroup), TabItem(独立A), TabItem(独立B), stretch]
        assert isinstance(outer[0], QFrame)
        assert outer[0].objectName() == "teamGroup"
        assert isinstance(outer[1], TabItem)
        assert isinstance(outer[2], TabItem)
        # stretch 在最末
        last = panel._list_layout.itemAt(panel._list_layout.count() - 1)
        assert last is not None and last.widget() is None

        # 团队容器内恰含 2 个成员 tab，且都在 _items 中
        inner = panel._team_groups["teamA"]._team_inner_layout
        inner_widgets = [inner.itemAt(i).widget() for i in range(inner.count())]
        assert len(inner_widgets) == 2
        assert all(w in panel._items for w in inner_widgets)

    def test_tab_join_team_stays_in_container_not_duplicated(self, panel):
        """新 tab 加入团队后：仍在容器内布局中、不在外层重复出现"""
        panel.add_tab("独立")
        panel.add_tab("新成员")
        panel.set_tab_team(1, "teamB")

        grp = panel._team_groups["teamB"]
        inner = grp._team_inner_layout
        inner_widgets = [inner.itemAt(i).widget() for i in range(inner.count())]
        assert panel._items[1] in inner_widgets, "加入团队后 tab 应在团队容器内"

        outer = _outer_widgets(panel._list_layout)
        assert panel._items[1] not in outer, "团队 tab 不应在外层布局重复出现"

        assert _stretch_count(panel._list_layout) == 1

    def test_rebuild_heals_missing_stretch(self, panel):
        """历史损坏（stretch 丢失）时 rebuild 自愈：addStretch 重新创建"""
        panel.add_tab("A")
        panel.add_tab("B")
        # 模拟历史损坏：把 stretch 从布局移除
        for i in range(panel._list_layout.count()):
            item = panel._list_layout.itemAt(i)
            if item is not None and item.widget() is None:
                panel._list_layout.takeAt(i)
                break
        assert _stretch_count(panel._list_layout) == 0

        # 触发 rebuild（团队归属变化 → 快照必变）
        panel.set_tab_team(0, "teamC")
        assert _stretch_count(panel._list_layout) == 1, "rebuild 应自愈补回 stretch"

    def test_remove_team_tab_rebuilds_layout(self, panel):
        """移除团队 tab 后：布局重建无重复、stretch 完好"""
        panel.add_tab("独立")
        panel.add_tab("团队-1")
        panel.add_tab("团队-2")
        panel.set_tab_team(1, "teamD")
        panel.set_tab_team(2, "teamD")

        panel.remove_tab(1)  # 移除团队 tab

        assert _stretch_count(panel._list_layout) == 1
        outer = _outer_widgets(panel._list_layout)
        assert len(outer) == len({id(w) for w in outer}), f"移除后存在重复 widget: {outer}"
        # 剩余团队 tab 仍在容器内
        grp = panel._team_groups["teamD"]
        inner = grp._team_inner_layout
        inner_widgets = [inner.itemAt(i).widget() for i in range(inner.count())]
        assert panel._items[1] in inner_widgets
        assert len(inner_widgets) == 1


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
