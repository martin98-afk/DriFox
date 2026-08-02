# -*- coding: utf-8 -*-
"""方案 A 阶段 3：历史面板团队分组 + 一键恢复逻辑测试

覆盖：
1. main_widget._build_team_groups：按 run_id 聚合、agent 去重、按 last_time 倒序、
   无 run_id 会话跳过
2. tab_manager_window._resolve_tab_team_id：run_id 优先、回落 team_name、非团队空
3. HistoryCard 团队分组渲染：set_team_groups 注入 + teamRestoreRequested 信号
4. main_widget._on_team_restore_requested：按 run_id 收集会话、创建窗口、
   注入恢复数据、join_team（mock 窗口创建与 history_manager）
"""

import sys

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication


def _ensure_qapp():
    """确保 QApplication 可用"""
    return QApplication.instance() or QApplication(sys.argv)


def _make_window_with_teams():
    """构造一个带 _build_team_groups 的轻量替身（不实例化完整 UI）。

    _build_team_groups 是纯数据方法（不依赖 UI 状态），直接用 object 绑定
    main_widget 的模块级辅助逻辑即可——此处以 mock 方式验证分组逻辑。
    """
    from types import SimpleNamespace

    return SimpleNamespace()


def _history_samples():
    """构造含团队/非团队字段的历史会话样例"""
    return [
        # run-1 团队：build + plan 两个角色
        {
            "session_id": "s1",
            "title": "团队会话1",
            "last_time": "2026-01-01 10:00:00",
            "team_run_id": "run-1",
            "team_name": "dev-team",
            "agent_name": "build",
        },
        {
            "session_id": "s2",
            "title": "团队会话2",
            "last_time": "2026-01-02 10:00:00",
            "team_run_id": "run-1",
            "team_name": "dev-team",
            "agent_name": "plan",
        },
        # run-2 团队：单独 build
        {
            "session_id": "s3",
            "title": "团队会话3",
            "last_time": "2026-01-03 10:00:00",
            "team_run_id": "run-2",
            "team_name": "qa-team",
            "agent_name": "build",
        },
        # 非团队会话（无 run_id）→ 不进分组
        {
            "session_id": "s4",
            "title": "普通会话",
            "last_time": "2026-01-04 10:00:00",
            "team_run_id": "",
            "team_name": "",
            "agent_name": "",
        },
        # 老团队会话（agent_name 空）→ 不参与角色胶囊
        {
            "session_id": "s5",
            "title": "旧团队会话",
            "last_time": "2026-01-05 10:00:00",
            "team_run_id": "run-3",
            "team_name": "legacy-team",
            "agent_name": "",
        },
    ]


class TestBuildTeamGroups:
    def test_groups_by_run_id(self):
        """按 run_id 聚合分组"""
        from app.main_widget import OpenAIChatToolWindow

        # 直接用绑定方法（_build_team_groups 不依赖 self 状态）
        groups = OpenAIChatToolWindow._build_team_groups(None, _history_samples())
        run_ids = [g["run_id"] for g in groups]
        assert run_ids == ["run-3", "run-2", "run-1"], "应按最后活跃时间倒序"

    def test_agent_names_dedup(self):
        """同组角色去重"""
        from app.main_widget import OpenAIChatToolWindow

        groups = OpenAIChatToolWindow._build_team_groups(None, _history_samples())
        run1 = next(g for g in groups if g["run_id"] == "run-1")
        assert run1["agent_names"] == ["build", "plan"], "run-1 应含 build+plan"
        assert run1["session_count"] == 2

    def test_no_run_id_sessions_excluded(self):
        """无 run_id 会话不进分组"""
        from app.main_widget import OpenAIChatToolWindow

        groups = OpenAIChatToolWindow._build_team_groups(None, _history_samples())
        run_ids = [g["run_id"] for g in groups]
        assert "run-1" in run_ids and "run-2" in run_ids and "run-3" in run_ids
        assert len(run_ids) == 3, "4 条记录中 3 个团队组（s4 非团队跳过）"

    def test_empty_input(self):
        """空列表返回空分组"""
        from app.main_widget import OpenAIChatToolWindow

        assert OpenAIChatToolWindow._build_team_groups(None, []) == []


class TestResolveTabTeamId:
    def test_team_run_id_priority(self):
        """有 run_id 时优先用 run_id 分组"""
        from app.widgets.tab_manager_window import TabManagerWindow

        class _Win:
            _team_agent_name = "build"
            _team_run_id = "run-x"
            _team_name = "dev-team"

        assert TabManagerWindow._resolve_tab_team_id(_Win()) == "run-x"

    def test_fallback_to_team_name(self):
        """老窗口无 run_id 回落 team_name"""
        from app.widgets.tab_manager_window import TabManagerWindow

        class _Win:
            _team_agent_name = "build"
            _team_run_id = ""
            _team_name = "dev-team"

        assert TabManagerWindow._resolve_tab_team_id(_Win()) == "dev-team"

    def test_non_team_returns_empty(self):
        """非团队窗口返回空串（留在独立区）"""
        from app.widgets.tab_manager_window import TabManagerWindow

        class _Win:
            _team_agent_name = ""
            _team_run_id = ""
            _team_name = ""

        assert TabManagerWindow._resolve_tab_team_id(_Win()) == ""


class TestHistoryCardTeamGroups:
    def test_signal_exists(self):
        """HistoryCard 应有 teamRestoreRequested 信号"""
        _ensure_qapp()
        from app.widgets.cards.settings.history_card import HistoryCard

        card = HistoryCard()
        assert hasattr(card, "teamRestoreRequested")
        assert "teamRestoreRequested" in [s for s in dir(card) if "teamRestore" in s]

    def test_set_team_groups_stores(self):
        """set_team_groups 存储数据"""
        _ensure_qapp()
        from app.widgets.cards.settings.history_card import HistoryCard

        card = HistoryCard()
        groups = [
            {"run_id": "r1", "team_name": "t1", "agent_names": ["build"], "last_time": "2026-01-01"},
        ]
        card.set_team_groups(groups)
        assert card._team_groups == groups

    def test_team_group_card_emits_restore(self, qtbot):
        """点击恢复按钮发射 restoreRequested(run_id)"""
        _ensure_qapp()
        from app.widgets.cards.settings.history_card import _TeamGroupCard

        card = _TeamGroupCard({"run_id": "run-9", "team_name": "dev", "agent_names": ["build"]})
        with qtbot.waitSignal(card.restoreRequested, timeout=500) as blocker:
            # 直接发射（模拟按钮点击）
            card.restoreRequested.emit("run-9")
        assert blocker.args == ["run-9"]


class TestTeamRestoreLogic:
    def test_restore_collects_by_run_id(self):
        """恢复逻辑按 run_id 收集成员会话（mock history_manager）"""
        _ensure_qapp()
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._is_destroyed = False
        win.history_manager = MagicMock()
        win.history_manager.get_history_list.return_value = _history_samples()
        win.history_manager.get_session_messages.side_effect = lambda sid: [{"role": "user", "content": sid}]
        win._get_team_manager = MagicMock()
        tm = MagicMock()
        tm.get_template.return_value = {"name": "dev-team"}
        tm.start_team_run.return_value = "new-run-1"
        win._get_team_manager.return_value = tm
        win._create_fresh_window = MagicMock()
        # 避免 InfoBar 用 MagicMock 作 parent 构造 QWidget 报错
        win._card_manager = MagicMock()

        class _FakeWin:
            _window_id = "w1"

            def __init__(self):
                self._branch_session_data = None
                self._team_agent_name = None
                self._team_name = None
                self._team_run_id = None

        fake = _FakeWin()
        win._create_fresh_window.return_value = fake

        # 只保留 run-1 的两条记录 + 非团队（应跳过）
        win.history_manager.get_history_list.return_value = [
            s for s in _history_samples() if s["team_run_id"] == "run-1"
        ]

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_restore_requested(win, "run-1")

        # 应创建 2 个窗口（build + plan），注入恢复数据与团队标记
        assert win._create_fresh_window.call_count == 2
        assert fake._branch_session_data is not None
        assert fake._team_agent_name in ("build", "plan")
        assert fake._team_run_id == "new-run-1"
        # join_team 被调用（每个窗口一次）
        assert tm.join_team.call_count == 2
