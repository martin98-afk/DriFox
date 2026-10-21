# -*- coding: utf-8 -*-
"""团队框折叠/展开回归测试

覆盖：
- 点击 header → inner 显隐翻转（修复前 ``collapsed = not inner.isVisible()``
  布尔写反，toggle 恒 no-op：展开点击仍展开、折叠点击仍折叠）
- 400ms 去抖到期后 app_state ``team_groups_collapsed`` 正确落盘（含反向展开）

风格对齐 tests/widgets/test_tab_team_project_ui.py；
app_state 隔离范式对齐 tests/core/test_welcome_plugin_tab.py::_isolated_app_state。
"""

from unittest.mock import patch

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest

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


class TestTeamCollapseToggle:
    """点击 header 切换团队框折叠/展开"""

    def _setup_team(self, panel, team_id="run_1"):
        idx = panel.add_tab("会话A")
        panel.set_tab_team(idx, team_id)
        grp = panel._team_groups[team_id]
        return idx, grp

    def test_click_header_toggles_collapse(self, panel, qtbot, isolated_app_state):
        """展开态点击 → 折叠；再点击 → 展开（修复前恒 no-op）"""
        _, grp = self._setup_team(panel)
        panel.show()
        qtbot.waitExposed(panel)

        header = grp._team_header
        inner = grp._team_inner_widget
        assert inner.isVisible(), "前置：初始应为展开态"

        QTest.mouseClick(header, Qt.LeftButton)
        assert not inner.isVisible(), "展开态点击后应折叠"

        QTest.mouseClick(header, Qt.LeftButton)
        assert inner.isVisible(), "折叠态再次点击应展开"

    def test_collapse_state_synced_to_app_state(self, panel, qtbot, isolated_app_state):
        """点击折叠/展开后 400ms 去抖到期，team_groups_collapsed 正确同步"""
        from app.utils import app_state as state

        _, grp = self._setup_team(panel)
        panel.show()
        qtbot.waitExposed(panel)

        QTest.mouseClick(grp._team_header, Qt.LeftButton)
        assert not grp._team_inner_widget.isVisible(), "前置：已折叠"
        qtbot.wait(550)  # 越过 400ms 去抖
        assert state.get("team_groups_collapsed", {}).get("run_1") is True, "折叠态应落盘 True"

        QTest.mouseClick(grp._team_header, Qt.LeftButton)
        assert grp._team_inner_widget.isVisible(), "前置：已展开"
        qtbot.wait(550)
        assert state.get("team_groups_collapsed", {}).get("run_1") is False, "展开态应落盘 False"

    def test_direct_set_collapsed_roundtrip(self, panel, isolated_app_state):
        """_set_team_collapsed 直调路径（右键菜单复用）显隐翻转正常"""
        _, grp = self._setup_team(panel)
        panel.show()

        assert grp._team_inner_widget.isVisible(), "前置：初始展开"
        panel._set_team_collapsed(grp, True)
        assert not grp._team_inner_widget.isVisible(), "应用折叠后成员层应隐藏"
        panel._set_team_collapsed(grp, False)
        assert grp._team_inner_widget.isVisible(), "应用展开后成员层应显示"
