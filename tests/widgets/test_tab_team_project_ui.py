# -*- coding: utf-8 -*-
"""#5b 团队框 UI 改造测试

覆盖：
- B1: 团队模式标签页 _icon_widget 隐藏、胶囊显示（set_team_mode + set_capsule 共存）
- B2: 非团队模式 _icon_widget 恢复显示（回归：行为与改动前一致）
- B5: update_tab_capsule/clear_tab_capsule 正常
- 附加（review 检查点 3/4）：
  - set_team_project header 项目 icon 值相等跳过（避免无效重绘）
  - refresh_style 不覆盖团队模式隐藏态

风格对齐 tests/widgets/test_tab_panel.py：panel/qtbot fixture + isHidden() 断言
（父链未显示时 isVisible() 恒 False，用显式隐藏标志 isHidden 判断）。
"""

from unittest.mock import patch

import pytest

from app.widgets.tab_panel import TabPanel


@pytest.fixture
def panel(qtbot):
    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    qtbot.addWidget(p)
    return p


class TestTeamModeTabItem:
    """B1/B2：团队模式标签页 icon 隐藏/恢复"""

    def test_team_mode_hides_icon_and_shows_capsule(self, panel):
        """B1: 团队模式 _icon_widget 隐藏、胶囊显示，二者共存。"""
        idx = panel.add_tab("会话A")
        panel.set_tab_team(idx, "run_1")
        panel.set_tab_team_mode(idx, True)

        assert panel._items[idx]._icon_widget.isHidden(), "团队模式项目 icon 应隐藏"
        # 胶囊照常显示（与 icon 隐藏共存）
        panel._items[idx].set_capsule("coder")
        assert not panel._items[idx]._capsule_label.isHidden(), "团队模式胶囊应显示"
        assert panel._items[idx]._capsule_label.text() == "coder"

    def test_non_team_mode_restores_icon(self, panel):
        """B2: 非团队模式 _icon_widget 恢复显示（回归不变）。"""
        idx = panel.add_tab("会话A")
        panel.set_tab_team_mode(idx, True)
        assert panel._items[idx]._icon_widget.isHidden()

        panel.set_tab_team_mode(idx, False)
        assert not panel._items[idx]._icon_widget.isHidden(), "非团队模式 icon 应恢复显示"

    def test_team_mode_exit_clears_capsule_and_restores_icon(self, panel):
        """B2 变体: 退出团队（refresh_capsule_for_window 路径）胶囊清除 + icon 恢复。"""
        idx = panel.add_tab("会话A")
        panel.set_tab_team(idx, "run_1")
        panel.set_tab_team_mode(idx, True)
        panel._items[idx].set_capsule("coder")

        # 模拟退出团队：清胶囊 + 恢复 icon
        panel._items[idx].clear_capsule()
        panel.set_tab_team_mode(idx, False)
        assert panel._items[idx]._capsule_label.isHidden(), "退出团队应清胶囊"
        assert not panel._items[idx]._icon_widget.isHidden(), "退出团队应恢复 icon"

    def test_refresh_style_keeps_team_mode_hidden(self, panel):
        """检查点 4: refresh_style 不得覆盖团队模式隐藏态。"""
        idx = panel.add_tab("会话A")
        panel.set_tab_team_mode(idx, True)
        panel.refresh_style()
        assert panel._items[idx]._icon_widget.isHidden(), "refresh_style 后隐藏态应保持"


class TestTeamHeaderIcon:
    """set_team_project：团队框 header 项目 icon"""

    def _setup_team(self, panel, team_id="run_1"):
        idx = panel.add_tab("会话A")
        panel.set_tab_team(idx, team_id)
        grp = panel._team_groups[team_id]
        return idx, grp, grp._team_icon

    def test_header_icon_hidden_by_default(self, panel):
        """新建团队框 header icon 默认隐藏（无团队级项目）。"""
        _, _, icon = self._setup_team(panel)
        assert icon is not None, "header 应存在项目 icon 控件"
        assert icon.isHidden(), "无团队级项目时 header icon 应隐藏"

    def test_header_icon_shows_team_project(self, panel):
        """设置团队级项目后 header icon 显示，缩写正确。"""
        _, _, icon = self._setup_team(panel)
        panel.set_team_project("run_1", "AB", "rgba(33,139,255,255)")
        assert not icon.isHidden(), "设置项目后 header icon 应显示"
        assert icon._initials == "AB"

    def test_header_icon_value_equal_skips_redraw(self, panel):
        """检查点 3: header icon 值相等跳过（不重复 set_project）。"""
        _, _, icon = self._setup_team(panel)
        panel.set_team_project("run_1", "AB", "rgba(33,139,255,255)")
        icon._initials = "STALE"  # 人为污染，若跳过则不被覆盖
        panel.set_team_project("run_1", "AB", "rgba(33,139,255,255)")
        assert icon._initials == "STALE", "值相等应跳过重绘"

    def test_header_icon_value_change_updates(self, panel):
        """值变化时 header icon 更新。"""
        _, _, icon = self._setup_team(panel)
        panel.set_team_project("run_1", "AB", "rgba(33,139,255,255)")
        panel.set_team_project("run_1", "CD", "rgba(1,2,3,255)")
        assert icon._initials == "CD", "值变化应更新"

    def test_header_icon_empty_hides(self, panel):
        """团队级项目为空（空串）时隐藏 header icon。"""
        _, _, icon = self._setup_team(panel)
        panel.set_team_project("run_1", "AB", "rgba(33,139,255,255)")
        panel.set_team_project("run_1", "", "")
        assert icon.isHidden(), "空项目应隐藏 header icon"


class TestCapsuleMethods:
    """B5: update_tab_capsule/clear_tab_capsule 正常"""

    def test_update_tab_capsule(self, panel):
        """B5: 更新胶囊显示角色名。"""
        idx = panel.add_tab("会话A")
        panel.update_tab_capsule(idx, "plan")
        assert not panel._items[idx]._capsule_label.isHidden(), "胶囊应显示"
        assert panel._items[idx]._capsule_label.text() == "plan"

    def test_clear_tab_capsule(self, panel):
        """B5: 清除胶囊隐藏并清空文本。"""
        idx = panel.add_tab("会话A")
        panel.update_tab_capsule(idx, "plan")
        panel.clear_tab_capsule(idx)
        assert panel._items[idx]._capsule_label.isHidden(), "清除后胶囊应隐藏"
        assert panel._items[idx]._capsule_label.text() == ""
