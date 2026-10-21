# -*- coding: utf-8 -*-
"""团队框 header 右键菜单回归测试

覆盖：
- 右键 header → 弹团队专属菜单（折叠/新建任务/快速新建成员/解散团队），
  不再错位回退到"当前选中 tab"菜单
- 菜单动作真实生效：折叠项切换显隐并经去抖落盘 app_state
- 文案随折叠态切换（折叠态显示"展开团队"）
- 解散走二级确认菜单，确认后 emit teamCloseRequested
- 右键 TabItem 菜单不受影响（含"切换会话"、无团队项）

右键模拟：直接向 panel sendEvent QContextMenuEvent（QTest 不会合成该事件），
globalPos 落在目标控件上 → contextMenuEvent 内部 childAt 命中检测被真实覆盖。
菜单 exec_ 以 monkeypatch stub（同步返回预设 action）：保留命中检测/菜单构建/
分发全真实链路，仅消除 exec_ 阻塞等待——定时器模拟键盘激活曾验证可行但存在
200ms 竞态（系统抖动下菜单提前关闭 → 迟到 fire 污染下一用例）。

风格对齐 tests/widgets/test_tab_panel_team_collapse.py；
app_state 隔离范式对齐 tests/core/test_welcome_plugin_tab.py::_isolated_app_state。
"""

from unittest.mock import patch

import pytest
from PyQt5.QtGui import QContextMenuEvent
from PyQt5.QtWidgets import QApplication, QMenu, QWidget

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


def _stub_menu_exec(monkeypatch, picks):
    """stub QMenu.exec_：逐层记录可选项文案并返回 picks 指定的 action

    picks 每项对应一层菜单返回的可选 action 序号（0-based，separator 跳过），
    超出 picks 的层返回 None（等价关闭菜单）。返回 layers 文案记录。
    """
    layers = []
    counter = {"i": 0}

    def fake_exec(self, pos):
        actions = [a for a in self.actions() if not a.isSeparator()]
        layers.append([a.text() for a in actions])
        i = counter["i"]
        counter["i"] += 1
        if i >= len(picks) or picks[i] is None:
            return None  # None = 只看菜单内容，不激活任何项
        return actions[picks[i]]

    monkeypatch.setattr(QMenu, "exec_", fake_exec)
    return layers


def _right_click(widget: QWidget):
    """向 panel 派发右键事件（globalPos 落在目标控件中心）"""
    panel = widget.window()
    gpos = widget.mapToGlobal(widget.rect().center())
    ev = QContextMenuEvent(QContextMenuEvent.Mouse, panel.mapFromGlobal(gpos), gpos)
    QApplication.sendEvent(panel, ev)


class TestTeamHeaderContextMenu:
    """右键团队框 header 弹团队菜单"""

    def _setup_team(self, panel, team_id="run_1"):
        idx = panel.add_tab("会话A")
        panel.set_tab_team(idx, team_id)
        grp = panel._team_groups[team_id]
        return idx, grp

    def test_header_menu_items_and_toggle_action(self, panel, qtbot, isolated_app_state, monkeypatch):
        """右键 header 弹 4 项团队菜单；触发"折叠团队"真实生效并经去抖落盘"""
        from app.utils import app_state as state

        _, grp = self._setup_team(panel)
        panel.show()
        qtbot.waitExposed(panel)

        layers = _stub_menu_exec(monkeypatch, picks=[0])  # 激活首项"折叠团队"
        _right_click(grp._team_header)

        assert len(layers) == 1 and len(layers[0]) == 4, f"团队菜单应含 4 项，实际 {layers}"
        assert layers[0] == ["折叠团队", "新建任务", "快速新建成员", "解散团队"]
        assert not grp._team_inner_widget.isVisible(), "触发折叠后成员层应隐藏"
        qtbot.wait(550)  # 越过 400ms 去抖
        assert state.get("team_groups_collapsed", {}).get("run_1") is True, "折叠态应落盘"

    def test_header_menu_text_follows_collapse_state(self, panel, qtbot, isolated_app_state, monkeypatch):
        """折叠态右键 → 首项文案为"展开团队"（文案随态切换）"""
        _, grp = self._setup_team(panel)
        panel.show()
        qtbot.waitExposed(panel)

        grp._team_inner_widget.setVisible(False)  # 预置折叠态

        layers = _stub_menu_exec(monkeypatch, picks=[0])  # 激活首项"展开团队"
        _right_click(grp._team_header)

        assert layers and layers[0][0] == "展开团队", f"折叠态首项应为展开团队，实际 {layers}"
        assert grp._team_inner_widget.isVisible(), "触发后应展开"

    def test_header_menu_disband_requires_confirm(self, panel, qtbot, isolated_app_state, monkeypatch):
        """解散走二级确认菜单：确认后 emit teamCloseRequested(team_id)"""
        _, grp = self._setup_team(panel)
        panel.show()
        qtbot.waitExposed(panel)

        # 主菜单激活第 4 项"解散团队" → 二级确认菜单激活首项"⚠ 确认解散团队"
        layers = _stub_menu_exec(monkeypatch, picks=[3, 0])
        with qtbot.waitSignal(panel.teamCloseRequested, timeout=5000) as blocked:
            _right_click(grp._team_header)

        assert blocked.args == ["run_1"], "确认后应携带 team_id 解散"
        assert len(layers) == 2, "应经历主菜单与确认菜单两层"
        assert layers[1][0] == "⚠ 确认解散团队"

    def test_header_menu_new_task_action(self, panel, qtbot, isolated_app_state, monkeypatch):
        """激活"新建任务"分支 → emit teamNewTaskRequested(team_id)"""
        _, grp = self._setup_team(panel)
        panel.show()
        qtbot.waitExposed(panel)

        layers = _stub_menu_exec(monkeypatch, picks=[1])
        with qtbot.waitSignal(panel.teamNewTaskRequested, timeout=5000) as blocked:
            _right_click(grp._team_header)

        assert blocked.args == ["run_1"], "新建任务应携带 team_id"
        assert layers[0][1] == "新建任务"

    def test_header_menu_add_member_action(self, panel, qtbot, isolated_app_state, monkeypatch):
        """激活"快速新建成员"分支 → emit teamAddMemberRequested(team_id)"""
        _, grp = self._setup_team(panel)
        panel.show()
        qtbot.waitExposed(panel)

        layers = _stub_menu_exec(monkeypatch, picks=[2])
        with qtbot.waitSignal(panel.teamAddMemberRequested, timeout=5000) as blocked:
            _right_click(grp._team_header)

        assert blocked.args == ["run_1"], "快速新建成员应携带 team_id"
        assert layers[0][2] == "快速新建成员"

    def test_tabitem_menu_unchanged(self, panel, qtbot, isolated_app_state, monkeypatch):
        """右键 TabItem 菜单行为不变：含"切换会话"、无团队项"""
        idx = panel.add_tab("会话B")
        panel.set_tab_team(idx, "run_1")  # TabItem 属于团队，但右键弹的仍是 tab 菜单
        panel.show()
        qtbot.waitExposed(panel)

        layers = _stub_menu_exec(monkeypatch, picks=[None])  # 只看菜单内容，不激活
        _right_click(panel._items[idx])

        assert layers, "TabItem 右键应有菜单"
        assert "切换会话" in layers[0], "TabItem 菜单应保留切换会话"
        assert "解散团队" not in layers[0] and "折叠团队" not in layers[0], "TabItem 菜单不应混入团队项"
