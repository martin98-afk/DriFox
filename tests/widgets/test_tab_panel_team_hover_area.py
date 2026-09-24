# -*- coding: utf-8 -*-
"""团队框 header hover 按钮占位回归测试

覆盖（#8 hover 三按钮固定占位）：
- hover 显示/隐藏三按钮（新建任务/快速新建成员/解散），badge.x 恒定不横移
  （无占位容器时按钮 hidden→visible 会挤压左侧元素，实测 badge.x 220→148）
- hover 前后 header 高度恒定
- 折叠态按钮占位容器整体隐藏（C3 窄条无空白）、展开态恢复

风格对齐 tests/widgets/test_tab_panel_team_project_ui.py 的横向 geometry 断言。
"""

from unittest.mock import patch

import pytest
from PyQt5.QtWidgets import QApplication

from app.widgets.tab_panel import TabPanel


@pytest.fixture
def isolated_app_state(tmp_path, monkeypatch):
    """app_state 隔离到 tmp_path（不读写真实缓存文件）"""
    from app.utils import app_state

    monkeypatch.setattr(app_state, "_state_file", lambda: tmp_path / "app_state.json")
    monkeypatch.setattr(app_state, "_cache", None)


@pytest.fixture
def panel(qtbot):
    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    # 团队框只在列表模式创建，显式 pin（避免本机树模式残留导致 KeyError）
    p.set_mode("list", persist=False)
    qtbot.addWidget(p)
    return p


class TestTeamHoverButtonArea:
    """hover 三按钮占位：显隐不重排"""

    def _setup_team(self, panel, team_id="run_1"):
        idx = panel.add_tab("会话A")
        panel.set_tab_team(idx, team_id)
        grp = panel._team_groups[team_id]
        return idx, grp

    def test_hover_buttons_keep_badge_x_constant(self, panel, qtbot, isolated_app_state):
        """三按钮显隐前后 badge.geometry().x() 恒定（占位容器吸收宽度变化）"""
        _, grp = self._setup_team(panel)
        panel.show()
        qtbot.waitExposed(panel)
        QApplication.processEvents()

        badge = grp._team_badge
        btn_area = grp._team_btn_area
        assert btn_area is not None and not btn_area.isHidden(), "占位容器展开态应始终可见"
        x_before = badge.geometry().x()

        # hover 进入：三按钮 hidden→visible
        grp._team_new_task_btn.setVisible(True)
        grp._team_add_btn.setVisible(True)
        grp._team_close_btn.setVisible(True)
        QApplication.processEvents()
        assert badge.geometry().x() == x_before, "hover 显示按钮后 badge 不得横移"

        # hover 离开：三按钮恢复隐藏
        grp._team_new_task_btn.setVisible(False)
        grp._team_add_btn.setVisible(False)
        grp._team_close_btn.setVisible(False)
        QApplication.processEvents()
        assert badge.geometry().x() == x_before, "hover 隐藏按钮后 badge 不得横移"

    def test_header_height_constant_across_hover(self, panel, qtbot, isolated_app_state):
        """hover 显隐前后 header 高度恒定（容器高度吸收按钮高度）"""
        _, grp = self._setup_team(panel)
        panel.show()
        qtbot.waitExposed(panel)
        QApplication.processEvents()

        header = grp._team_header
        h_before = header.height()
        assert h_before > 0, "布局生效后 header 应有实际高度"

        grp._team_new_task_btn.setVisible(True)
        grp._team_add_btn.setVisible(True)
        grp._team_close_btn.setVisible(True)
        QApplication.processEvents()
        assert header.height() == h_before, "hover 显示按钮后 header 高度不得变化"

        grp._team_new_task_btn.setVisible(False)
        grp._team_add_btn.setVisible(False)
        grp._team_close_btn.setVisible(False)
        QApplication.processEvents()
        assert header.height() == h_before, "hover 隐藏按钮后 header 高度不得变化"

    def test_compact_mode_hides_btn_area(self, panel, qtbot, isolated_app_state):
        """折叠态占位容器整体隐藏（窄条无按钮区空白），展开态恢复"""
        _, grp = self._setup_team(panel)
        panel.show()
        qtbot.waitExposed(panel)

        panel._apply_team_compact(grp, True)
        assert grp._team_btn_area.isHidden(), "折叠态按钮占位容器应隐藏"

        panel._apply_team_compact(grp, False)
        assert not grp._team_btn_area.isHidden(), "展开态按钮占位容器应恢复"

    def test_idle_state_no_persistent_blank(self, panel, qtbot, isolated_app_state):
        """#11 回归：非 hover 空闲态不得有常驻空白区

        修复前 btn_area setFixedSize(68,20) 恒占位 → header badge 右侧 68px
        空白裸露 + grp 最小宽被撑到 154（窄侧边栏整框被撑宽，行右缘出空白）。
        修复后：空闲态按钮区零宽度、badge 定宽锚贴 header 右缘、grp 最小宽回落。
        """
        _, grp = self._setup_team(panel)
        panel.show()
        qtbot.waitExposed(panel)
        QApplication.processEvents()

        header = grp._team_header
        badge = grp._team_badge
        btn_area = grp._team_btn_area

        # 空闲态按钮区零宽度（不占位，hover 才展开从标题借空间）
        assert btn_area.width() <= 1, f"空闲态按钮区应零占位，实际 {btn_area.width()}px（#11 常驻空白回归）"
        # grp 最小宽回落：窄侧边栏不再被撑出横向空白
        assert grp.minimumSizeHint().width() <= 120, (
            f"grp 最小宽应回落（回归时 154），实际 {grp.minimumSizeHint().width()}"
        )
        # badge 定宽锚贴 header 右缘（留 1px 呼吸位），不再缩进 68px 空白里
        badge_right = badge.geometry().x() + badge.width()
        assert abs(badge_right - header.width()) <= 2, (
            f"badge 应贴 header 右缘：badge_right={badge_right} header_w={header.width()}"
        )
