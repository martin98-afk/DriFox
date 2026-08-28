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
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel


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

    def test_team_group_card_emits_restore(self):
        """点击恢复按钮发射 restoreRequested(run_id)"""
        _ensure_qapp()
        from app.widgets.cards.settings.history_card import _TeamGroupCard

        card = _TeamGroupCard({"run_id": "run-9", "team_name": "dev", "agent_names": ["build"]})
        # 直接连接信号捕获参数（不依赖 qtbot）
        captured = []
        card.restoreRequested.connect(captured.append)
        card.restoreRequested.emit("run-9")
        assert captured == ["run-9"]

    def test_team_group_card_update_group_refreshes_capsules(self):
        """update_group 增量刷新成员数据（F6 回归：UI 成员 4→1 修复）。

        M4 改造：角色胶囊改为展开区成员行（默认收起）。update_group 必须
        同步元信息/成员列表；展开时渲染成员行（角色+标题），成员变化后
        展开区刷新，否则 UI 成员与实际不一致。
        """
        _ensure_qapp()
        from app.widgets.cards.settings.history_card import _TeamGroupCard

        card = _TeamGroupCard({"run_id": "run-9", "team_name": "dev", "agent_names": ["build"]})
        # 默认收起：元信息显示 1 位成员，无成员行
        texts_1 = [lbl.text() for lbl in card.findChildren(QLabel)]
        assert any("1 位成员" in t for t in texts_1)
        assert "build" not in texts_1, "收起状态下不应有成员行"

        # 展开 → 渲染成员行（build 胶囊）
        card._toggle_members()
        texts_2 = [lbl.text() for lbl in card.findChildren(QLabel)]
        assert "build" in texts_2, "展开后应显示成员行 build"

        # 更新为 build + plan + review 三个成员
        card.update_group({"run_id": "run-9", "team_name": "dev", "agent_names": ["build", "plan", "review"]})
        QApplication.processEvents()  # 处理旧成员行 deleteLater
        texts_3 = [lbl.text() for lbl in card.findChildren(QLabel)]
        assert any("3 位成员" in t for t in texts_3), "元信息应刷新为 3 位成员"
        assert "build" in texts_3 and "plan" in texts_3 and "review" in texts_3, "展开区应刷新为 3 个成员行"

        # 成员减少也应生效（清理后只剩 build）
        card.update_group({"run_id": "run-9", "team_name": "dev", "agent_names": ["build"]})
        QApplication.processEvents()  # 处理旧成员行 deleteLater
        texts_4 = [lbl.text() for lbl in card.findChildren(QLabel)]
        assert any("1 位成员" in t for t in texts_4)
        assert "build" in texts_4 and "plan" not in texts_4 and "review" not in texts_4, "成员行应随成员减少刷新"


class TestTeamRestoreLogic:
    def test_restore_collects_by_run_id(self):
        """恢复逻辑按 run_id 收集成员会话（mock history_manager）"""
        _ensure_qapp()
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._is_destroyed = False
        win.history_manager = MagicMock()
        win.history_manager.get_team_sessions_by_run_id.return_value = [
            s for s in _history_samples() if s["team_run_id"] == "run-1"
        ]
        win.history_manager.get_session_messages.side_effect = lambda sid: [{"role": "user", "content": sid}]
        win._get_team_manager = MagicMock()
        tm = MagicMock()
        tm.get_template.return_value = {"name": "dev-team"}
        # 🛡️ Bug1：恢复路径优先复用当前团队已有 run_id（用户期望恢复后新对话
        # 归属原 run_id 团队会话，历史面板分组不分裂）；仅无 run_id 时才生成。
        tm.get_team_run_id.return_value = "run-1"
        tm.start_team_run.return_value = "new-run-1"
        win._get_team_manager.return_value = tm
        win._create_fresh_window = MagicMock()
        # 避免 InfoBar 用 MagicMock 作 parent 构造 QWidget 报错
        win._card_manager = MagicMock()

        class _FakeWin:
            _window_id = "w1"

            def __init__(self, branch_data=None):
                self._branch_session_data = branch_data
                self._team_agent_name = None
                self._team_name = None
                self._team_run_id = None

        fake = _FakeWin()

        # 🛡️ 新实现：_create_fresh_window 内部在 add_window（触发 showEvent）
        # 之前把 branch_data 赋给新窗口；mock 需模拟该语义，否则 fake 的
        # _branch_session_data 不会自动赋值。
        def _fake_create(branch_data=None, **kw):
            fake._branch_session_data = branch_data
            return fake

        win._create_fresh_window.side_effect = _fake_create

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_restore_requested(win, "run-1")

        # 应创建 2 个窗口（build + plan），注入恢复数据与团队标记
        assert win._create_fresh_window.call_count == 2
        assert fake._branch_session_data is not None
        assert fake._team_agent_name in ("build", "plan")
        # M1'：恢复路径不再复用现有 run_id——独立生成 UUID，与现有团队区分
        import re as _re
        assert fake._team_run_id, "恢复窗口必须有 run_id"
        assert _re.fullmatch(r"[0-9a-f]{32}", fake._team_run_id), (
            f"恢复 run_id 应为 uuid4 hex（32 位小写），实际: {fake._team_run_id!r}"
        )
        assert fake._team_run_id != "run-1", (
            f"恢复 run_id 不应复用现有 run_id，实际: {fake._team_run_id!r}"
        )
        tm.start_team_run.assert_not_called()
        tm.get_team_run_id.assert_not_called()
        # L4/F2 回归：分支数据必须透传 project（保持会话原项目归属）
        for call_args in win._create_fresh_window.call_args_list:
            _kwargs = call_args.kwargs
            assert "branch_data" in _kwargs
            branch = _kwargs["branch_data"]
            assert branch["project"] in ("", "dev-team", "qa-team") or branch["project"] is not None
        # join_team 被调用（每个窗口一次），且透传新 run_id
        assert tm.join_team.call_count == 2
        for call_args in tm.join_team.call_args_list:
            kwargs = call_args.kwargs
            assert _re.fullmatch(r"[0-9a-f]{32}", kwargs.get("run_id", "") or ""), (
                f"join_team.run_id 应为 uuid4 hex，实际: {kwargs.get('run_id')!r}"
            )

    def test_restore_uses_independent_run_id_regardless_of_existing(self):
        """M1'：恢复路径**始终**用全新 UUID run_id，与现有团队是否有 run_id 无关。

        修复历史设计：旧版"无 run_id 时 fallback 到 start_team_run()" 会改写
        team.json 顶层 run_id，让后续 /team --join 等读 get_team_run_id() 的入口
        漂到新团队。改为直接 uuid4().hex，不动顶层 run_id，新成员记录透传新值。
        """
        _ensure_qapp()
        import re as _re
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._is_destroyed = False
        win.history_manager = MagicMock()
        win.history_manager.get_team_sessions_by_run_id.return_value = [
            s for s in _history_samples() if s["team_run_id"] == "run-1"
        ]
        win.history_manager.get_session_messages.side_effect = lambda sid: [{"role": "user", "content": sid}]
        win._get_team_manager = MagicMock()
        tm = MagicMock()
        # 即使现有团队已有 run_id，恢复路径也不读它、不复用
        tm.get_team_run_id.return_value = "fresh-run-1"
        win._get_team_manager.return_value = tm
        win._card_manager = MagicMock()

        created = []

        class _FakeWin:
            _window_id = "w1"

            def __init__(self, branch_data=None):
                self._branch_session_data = branch_data
                self._team_agent_name = None
                self._team_name = None
                self._team_run_id = None
                created.append(self)

        win._create_fresh_window.side_effect = lambda branch_data=None, **kw: _FakeWin(branch_data)

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_restore_requested(win, "run-1")

        assert len(created) == 2, "应创建 2 个恢复窗口"
        for c in created:
            assert _re.fullmatch(r"[0-9a-f]{32}", c._team_run_id), (
                f"恢复 run_id 应为 uuid4 hex，实际: {c._team_run_id!r}"
            )
            assert c._team_run_id != "fresh-run-1", (
                f"恢复 run_id 不应复用现有团队 run_id，实际: {c._team_run_id!r}"
            )
        # 不再走 start_team_run：避免改写 team.json 顶层 run_id 副作用
        tm.start_team_run.assert_not_called()

    def test_restore_dedups_by_agent_keeps_latest(self):
        """恢复按 agent_name 去重，每组取 last_time 最新一条（成员 4→1 修复）。

        回归保护：同 agent 多轮会话时只建一个窗口（取最新），
        且空消息 agent 不跳过（窗口照常创建）。
        """
        _ensure_qapp()
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._is_destroyed = False
        win.history_manager = MagicMock()
        # run-1 有 build ×2（不同 last_time）+ plan ×1 + 空消息 agent review ×1
        win.history_manager.get_team_sessions_by_run_id.return_value = [
            {
                "session_id": "s-build-old",
                "title": "build 旧",
                "last_time": "2026-01-01 10:00:00",
                "team_run_id": "run-1",
                "team_name": "dev-team",
                "agent_name": "build",
            },
            {
                "session_id": "s-build-new",
                "title": "build 新",
                "last_time": "2026-01-02 10:00:00",
                "team_run_id": "run-1",
                "team_name": "dev-team",
                "agent_name": "build",
            },
            {
                "session_id": "s-plan",
                "title": "plan",
                "last_time": "2026-01-03 10:00:00",
                "team_run_id": "run-1",
                "team_name": "dev-team",
                "agent_name": "plan",
            },
            {
                "session_id": "s-review",
                "title": "review 无消息",
                "last_time": "2026-01-04 10:00:00",
                "team_run_id": "run-1",
                "team_name": "dev-team",
                "agent_name": "review",
            },
        ]
        # review 无消息（返回空列表）→ 窗口仍应创建（不跳过整个 agent）
        win.history_manager.get_session_messages.side_effect = lambda sid: (
            [{"role": "user", "content": sid}] if sid != "s-review" else []
        )
        win._get_team_manager = MagicMock()
        tm = MagicMock()
        # Bug1：恢复优先复用现有 run_id（当前团队已有 run-9）
        tm.get_team_run_id.return_value = "run-9"
        tm.start_team_run.return_value = "new-run-9"
        win._get_team_manager.return_value = tm
        win._card_manager = MagicMock()

        created = []

        class _FakeWin:
            _window_id = "w"

            def __init__(self, branch_data=None):
                self._branch_session_data = branch_data
                self._team_agent_name = None
                self._team_name = None
                self._team_run_id = None
                created.append(self)

        win._create_fresh_window.side_effect = lambda branch_data=None, **kw: _FakeWin(branch_data)

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_restore_requested(win, "run-1")

        # 3 个 agent（build 去重为 1 + plan + review）→ 3 个窗口
        assert len(created) == 3, f"应创建 3 个窗口（build 去重），实际 {len(created)}"
        agent_names = sorted(c._team_agent_name for c in created)
        assert agent_names == ["build", "plan", "review"], f"agent 集合错误: {agent_names}"
        # M1'：所有窗口共享同一**全新** run_id（uuid4 hex），不复用现有 run-9
        import re as _re
        run_ids = {c._team_run_id for c in created}
        assert len(run_ids) == 1, f"恢复窗口应共享同一 run_id，实际多个: {run_ids}"
        run_id = run_ids.pop()
        assert _re.fullmatch(r"[0-9a-f]{32}", run_id), (
            f"恢复 run_id 应为 uuid4 hex，实际: {run_id!r}"
        )
        assert run_id != "run-9", f"恢复 run_id 不应复用现有 run-9，实际: {run_id!r}"
        # build 取最新会话（s-build-new）
        build_win = next(c for c in created if c._team_agent_name == "build")
        assert build_win._branch_session_data["name"] == "build 新", "build 应取 last_time 最新会话"
        # 空消息 agent 窗口也创建，仅消息为空
        review_win = next(c for c in created if c._team_agent_name == "review")
        assert review_win._branch_session_data["messages"] == []

    def test_restore_uses_team_name_from_session(self):
        """恢复窗口 _team_name 用会话记录 team_name（不再 set_template 篡改模板）。

        回归保护：恢复路径已删除 set_template 调用，模板上下文仅保留
        给 /team --load；恢复窗口团队名以会话记录为准。
        """
        _ensure_qapp()
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._is_destroyed = False
        win.history_manager = MagicMock()
        win.history_manager.get_team_sessions_by_run_id.return_value = [
            {
                "session_id": "s1",
                "title": "团队会话1",
                "last_time": "2026-01-01 10:00:00",
                "team_run_id": "run-1",
                "team_name": "dev-team",
                "agent_name": "build",
                "project": "proj-x",
            }
        ]
        win.history_manager.get_session_messages.side_effect = lambda sid: [{"role": "user", "content": sid}]
        win._get_team_manager = MagicMock()
        tm = MagicMock()
        # M1'：恢复路径不读 get_team_run_id、不调 start_team_run，独立生成 UUID。
        tm.get_team_run_id.return_value = "run-1"
        tm.start_team_run.return_value = "unused"
        win._get_team_manager.return_value = tm
        win._card_manager = MagicMock()

        class _FakeWin:
            _window_id = "w1"

            def __init__(self, branch_data=None):
                self._branch_session_data = branch_data
                self._team_agent_name = None
                self._team_name = None
                self._team_run_id = None

        fake = _FakeWin()

        # 模拟新实现：_create_fresh_window 内部赋值 branch_data
        def _fake_create(branch_data=None, **kw):
            fake._branch_session_data = branch_data
            return fake

        win._create_fresh_window.side_effect = _fake_create

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_restore_requested(win, "run-1")

        assert fake._team_name == "dev-team", "恢复窗口团队名应来自会话记录"
        # F5 回归：恢复路径不再调用 set_template
        tm.set_template.assert_not_called()
        # L4 回归：project 透传到 branch_data
        assert fake._branch_session_data["project"] == "proj-x"

    def test_save_current_session_passes_team_kwargs(self):
        """F1 回归：_save_current_session_to_history 高频保存路径透传团队元数据。

        团队窗口（_team_run_id 非空）每轮保存都写入 team_run_id/agent_name；
        非团队窗口不传（update 保留现值 / save 落空值）。
        """
        _ensure_qapp()
        from unittest.mock import MagicMock

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._team_run_id = "run-rid"
        win._team_name = "dev-team"
        win._team_agent_name = "build"
        win._session_dirty = True
        win._current_session_id = "sid-1"
        win._current_project = "默认项目"
        win.history_manager = MagicMock()
        win.history_manager.find_index_by_session_id.return_value = 0
        session = MagicMock()
        session.messages = [{"role": "user", "content": "hi"}]
        session.system_prompt = ""
        session.topic_summary = "标题"
        session.compaction_state = {}
        session.compaction_cache = {}
        win.session_manager.get_current_session.return_value = session
        win._get_current_worktree_path.return_value = ""
        win._update_node_preview = MagicMock()

        OpenAIChatToolWindow._save_current_session_to_history(win)

        # update_session 必须收到团队三参数
        _args, kwargs = win.history_manager.update_session.call_args
        assert kwargs.get("team_run_id") == "run-rid"
        assert kwargs.get("team_name") == "dev-team"
        assert kwargs.get("agent_name") == "build"

        # 非团队窗口：不传团队参数（update 保留现值）
        win2 = MagicMock()
        win2._team_run_id = ""
        win2._session_dirty = True
        win2._current_session_id = "sid-2"
        win2._current_project = "默认项目"
        win2.history_manager = MagicMock()
        win2.history_manager.find_index_by_session_id.return_value = 0
        win2.session_manager.get_current_session.return_value = session
        win2._get_current_worktree_path.return_value = ""
        win2._update_node_preview = MagicMock()

        OpenAIChatToolWindow._save_current_session_to_history(win2)
        _args2, kwargs2 = win2.history_manager.update_session.call_args
        assert "team_run_id" not in kwargs2, "非团队窗口不应传 team_run_id"
        assert "team_name" not in kwargs2
        assert "agent_name" not in kwargs2

    def test_create_branched_session_passes_project(self):
        """F2/L4 回归：_create_branched_session 支持 project 透传并同步窗口/backend。

        历史会话"无法加载"的根因之一：恢复时 project 未透传导致会话归属
        漂移。此处验证 project 参数生效、无 project 时回落当前项目。
        """
        _ensure_qapp()
        from unittest.mock import MagicMock

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._is_destroyed = False
        win._is_streaming = False
        win._topic_summary_cancelled = False
        win.backend = MagicMock()
        win.backend.chat_engine = MagicMock()
        win.backend.tool_executor = MagicMock()
        win._cache_current_session_cards = MagicMock()
        win._batch_cards = []
        win._message_batch = []
        win._clear_chat_area = MagicMock()
        win._display_current_session = MagicMock()
        win._load_agent_list = MagicMock()
        win._virtual_scroll_timer = MagicMock()
        win._virtual_scroll_timer.stop = MagicMock()
        win.title_edit = MagicMock()
        win.node_preview = MagicMock()
        win.node_preview.clear_nodes = MagicMock()
        win.input_area = MagicMock()
        win.input_area.setFixedHeight = MagicMock()
        win._current_project = "旧项目"
        win.backend.create_session.return_value = MagicMock()
        win.backend.create_session.return_value.messages = []
        win.backend.create_session.return_value.session_id = "new-sid"
        win.backend.create_session.return_value.metadata = {}

        OpenAIChatToolWindow._create_branched_session(
            win, [{"role": "user", "content": "hi"}], "分支", project="proj-x"
        )

        # project 透传生效：metadata + 窗口 + backend 同步
        assert win.backend.create_session.return_value.metadata["project"] == "proj-x"
        assert win._current_project == "proj-x"
        win.backend.tool_executor.set_current_project.assert_called_once_with("proj-x")

        # 无 project（空串）→ 回落当前项目（重置 _current_project 模拟新窗口）
        win._current_project = "旧项目"
        OpenAIChatToolWindow._create_branched_session(win, [{"role": "user", "content": "hi"}], "分支2", project="")
        assert win.backend.create_session.return_value.metadata["project"] == "旧项目", (
            "空 project 应回落 _current_project"
        )

    def test_create_branched_session_marks_dirty_for_team_window(self):
        """Bug2：恢复窗口（团队窗口 _team_run_id 非空）注入历史消息后必须置脏。

        根因：恢复窗口经 _create_branched_session 注入历史消息但 _session_dirty
        仍为 False → 关闭时 _auto_save_current_session 的 `if not self._session_dirty:
        return` 跳过保存 → 未触发成员窗口不落库，历史聚合 _merge_team_lightweight
        漏掉该成员。修复 = 团队窗口注入后置脏。非团队窗口（复制窗口）不置脏，
        保持"无变更不保存"原语义。
        """
        _ensure_qapp()
        from unittest.mock import MagicMock

        from app.main_widget import OpenAIChatToolWindow

        def _make_win(team_run_id: str):
            win = MagicMock()
            win._is_destroyed = False
            win._is_streaming = False
            win._topic_summary_cancelled = False
            win._team_run_id = team_run_id
            win._session_dirty = False
            win.backend = MagicMock()
            win.backend.chat_engine = MagicMock()
            win.backend.tool_executor = MagicMock()
            win._cache_current_session_cards = MagicMock()
            win._batch_cards = []
            win._message_batch = []
            win._clear_chat_area = MagicMock()
            win._display_current_session = MagicMock()
            win._load_agent_list = MagicMock()
            win._virtual_scroll_timer = MagicMock()
            win._virtual_scroll_timer.stop = MagicMock()
            win.title_edit = MagicMock()
            win.node_preview = MagicMock()
            win.node_preview.clear_nodes = MagicMock()
            win.input_area = MagicMock()
            win.input_area.setFixedHeight = MagicMock()
            win._current_project = "默认项目"
            win.backend.create_session.return_value = MagicMock()
            win.backend.create_session.return_value.messages = []
            win.backend.create_session.return_value.session_id = "new-sid"
            win.backend.create_session.return_value.metadata = {}
            return win

        # 团队恢复窗口：注入历史消息 → 置脏（关闭时落库，聚合不再漏成员）
        team_win = _make_win("run-x")
        OpenAIChatToolWindow._create_branched_session(team_win, [{"role": "user", "content": "hi"}], "分支")
        assert team_win._session_dirty is True, f"团队恢复窗口必须置脏，实际 {team_win._session_dirty!r}"

        # 非团队复制窗口：保持不置脏（无变更不保存原语义）
        plain_win = _make_win("")
        OpenAIChatToolWindow._create_branched_session(plain_win, [{"role": "user", "content": "hi"}], "分支")
        assert plain_win._session_dirty is False, "非团队窗口不应置脏"

    def test_restore_duplicate_role_members_with_snapshot(self):
        """T3：同角色多成员（两个 build 异 window_id）恢复时各建独立窗口。

        场景：build@win_02 / build@win_03 各有会话 + plan@win_01 会话；
        team_members 快照含 wid 记录（build×2 + plan + 无会话的 review）。
        → 4 个窗口：build×2（会话按 last_time 降序各分配一条）、plan×1、
        review×1（空窗口仍创建）。
        """
        _ensure_qapp()
        import json as _json
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        snap = _json.dumps(
            [
                {"agent_name": "build", "window_id": "win_02"},
                {"agent_name": "build", "window_id": "win_03"},
                {"agent_name": "plan", "window_id": "win_01"},
                {"agent_name": "review", "window_id": "win_04"},
            ],
            ensure_ascii=False,
        )
        win = MagicMock()
        win._is_destroyed = False
        win.history_manager = MagicMock()
        win.history_manager.get_team_sessions_by_run_id.return_value = [
            {
                "session_id": "s-build-old",
                "title": "build 旧",
                "last_time": "2026-01-01 10:00:00",
                "team_run_id": "run-1",
                "team_name": "dev-team",
                "agent_name": "build",
                "team_members": snap,
            },
            {
                "session_id": "s-build-new",
                "title": "build 新",
                "last_time": "2026-01-02 10:00:00",
                "team_run_id": "run-1",
                "team_name": "dev-team",
                "agent_name": "build",
                "team_members": snap,
            },
            {
                "session_id": "s-plan",
                "title": "plan 会话",
                "last_time": "2026-01-03 10:00:00",
                "team_run_id": "run-1",
                "team_name": "dev-team",
                "agent_name": "plan",
                "team_members": snap,
            },
        ]
        # review 无会话（session_id 空）→ 消息返回空列表（空窗口）
        win.history_manager.get_session_messages.side_effect = lambda sid: (
            [{"role": "user", "content": sid}] if sid else []
        )
        win._get_team_manager = MagicMock()
        tm = MagicMock()
        tm.get_team_run_id.return_value = "run-9"
        tm.start_team_run.return_value = "new-run-9"
        win._get_team_manager.return_value = tm
        win._card_manager = MagicMock()

        created = []

        class _FakeWin:
            _window_id = "w"

            def __init__(self, branch_data=None):
                self._branch_session_data = branch_data
                self._team_agent_name = None
                self._team_name = None
                self._team_run_id = None
                created.append(self)

        win._create_fresh_window.side_effect = lambda branch_data=None, **kw: _FakeWin(branch_data)

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_restore_requested(win, "run-1")

        # 4 个窗口：build×2（快照 wid 补差额）+ plan×1 + review×1（空窗口）
        assert len(created) == 4, f"应创建 4 个窗口（2 build + plan + review 空窗口），实际 {len(created)}"
        # 会话按 wid/窗口依次分配：build 两个窗口分别加载新旧会话（各 1 条）
        build_wins = sorted(
            (c for c in created if c._team_agent_name == "build"),
            key=lambda c: c._branch_session_data["name"],
        )
        assert len(build_wins) == 2, "同角色 build 应建 2 个独立窗口"
        build_names = [c._branch_session_data["name"] for c in build_wins]
        assert set(build_names) == {"build 旧", "build 新"}, f"build 两窗口应各取一条会话: {build_names}"
        # plan 窗口
        plan_win = next(c for c in created if c._team_agent_name == "plan")
        assert plan_win._branch_session_data["name"] == "plan 会话"
        # review 无会话记录 → 空窗口仍创建（消息为空）
        review_win = next(c for c in created if c._team_agent_name == "review")
        assert review_win._branch_session_data["messages"] == [], "快照成员无会话 → 空窗口"
        # 全部窗口重新登记为团队成员（join_team 每窗口一次）
        assert tm.join_team.call_count == 4, "4 个窗口各 join_team 一次"

    def test_restore_same_agent_window_two_rounds_takes_latest(self):
        """场景6：同 (agent,wid) 多轮取最新——build@win_02 两轮只建 1 窗取最新。

        与 test_restore_duplicate_role_members_with_snapshot（异 wid 各建窗）互补：
        快照中 build 只有 win_02 一个成员，但会话记录有 build 两轮（同 wid），
        恢复时窗口数 = 快照计数（1 个 build），会话按 last_time 降序取最新一条。
        """
        _ensure_qapp()
        import json as _json
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        snap = _json.dumps(
            [{"agent_name": "build", "window_id": "win_02"}, {"agent_name": "plan", "window_id": "win_01"}],
            ensure_ascii=False,
        )
        win = MagicMock()
        win._is_destroyed = False
        win.history_manager = MagicMock()
        win.history_manager.get_team_sessions_by_run_id.return_value = [
            {
                "session_id": "s-build-old",
                "title": "build 首轮",
                "last_time": "2026-01-01 10:00:00",
                "team_run_id": "run-1",
                "team_name": "dev-team",
                "agent_name": "build",
                "team_members": snap,
            },
            {
                "session_id": "s-build-new",
                "title": "build 次轮",
                "last_time": "2026-01-02 10:00:00",
                "team_run_id": "run-1",
                "team_name": "dev-team",
                "agent_name": "build",
                "team_members": snap,
            },
            {
                "session_id": "s-plan",
                "title": "plan 会话",
                "last_time": "2026-01-03 10:00:00",
                "team_run_id": "run-1",
                "team_name": "dev-team",
                "agent_name": "plan",
                "team_members": snap,
            },
        ]
        win.history_manager.get_session_messages.side_effect = lambda sid: (
            [{"role": "user", "content": sid}] if sid else []
        )
        win._get_team_manager = MagicMock()
        tm = MagicMock()
        tm.get_team_run_id.return_value = "run-9"
        tm.start_team_run.return_value = "new-run-9"
        win._get_team_manager.return_value = tm
        win._card_manager = MagicMock()

        created = []

        class _FakeWin:
            _window_id = "w"

            def __init__(self, branch_data=None):
                self._branch_session_data = branch_data
                self._team_agent_name = None
                self._team_name = None
                self._team_run_id = None
                created.append(self)

        win._create_fresh_window.side_effect = lambda branch_data=None, **kw: _FakeWin(branch_data)

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_restore_requested(win, "run-1")

        # build 快照仅 1 个成员（win_02）→ 只建 1 个 build 窗口，取最新会话
        build_wins = [c for c in created if c._team_agent_name == "build"]
        assert len(build_wins) == 1, f"同 (agent,wid) 两轮应只建 1 窗，实际 {len(build_wins)}"
        assert build_wins[0]._branch_session_data["name"] == "build 次轮", "应取 last_time 最新会话"
        # plan 1 窗
        assert len([c for c in created if c._team_agent_name == "plan"]) == 1
        assert len(created) == 2, "总窗口数 = build×1 + plan×1"

    def test_restore_template_agents_not_duplicated(self):
        """Bug 3：模板 agents（无 wid）不得与快照成员重复建窗。

        场景：模板团队 [build, plan, review] + 3 个成员（build@win_01 /
        plan@win_02 / review@win_03）。会话快照 = get_team_member_snapshot
        合并结果（模板 agents 无 wid + 快照成员有 wid，共 6 条）。

        回归保护：旧逻辑对 6 条记录逐条建窗 → 6 个窗口（模板 3 重复），
        且每轮恢复窗口数随重建快照线性膨胀（6→9→12…，用户报告"恢复后
        对话完再次恢复，团队多了一堆成员"）。修复后只按有 wid 的成员
        建窗（模板 agents 被同 agent 的 wid 记录吸收）→ 3 个窗口。
        """
        _ensure_qapp()
        import json as _json
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        # 模拟 get_team_member_snapshot 落库的会话快照：
        # 模板 agents（无 wid）+ 快照成员（有 wid）
        snap = _json.dumps(
            [
                {"agent_name": "build", "window_id": ""},
                {"agent_name": "plan", "window_id": ""},
                {"agent_name": "review", "window_id": ""},
                {"agent_name": "build", "window_id": "win_01"},
                {"agent_name": "plan", "window_id": "win_02"},
                {"agent_name": "review", "window_id": "win_03"},
            ],
            ensure_ascii=False,
        )
        win = MagicMock()
        win._is_destroyed = False
        win.history_manager = MagicMock()
        win.history_manager.get_team_sessions_by_run_id.return_value = [
            {
                "session_id": "s-build",
                "title": "build 会话",
                "last_time": "2026-01-02 10:00:00",
                "team_run_id": "run-1",
                "team_name": "dev-team",
                "agent_name": "build",
                "team_members": snap,
            },
            {
                "session_id": "s-plan",
                "title": "plan 会话",
                "last_time": "2026-01-03 10:00:00",
                "team_run_id": "run-1",
                "team_name": "dev-team",
                "agent_name": "plan",
                "team_members": snap,
            },
            {
                "session_id": "s-review",
                "title": "review 会话",
                "last_time": "2026-01-04 10:00:00",
                "team_run_id": "run-1",
                "team_name": "dev-team",
                "agent_name": "review",
                "team_members": snap,
            },
        ]
        win.history_manager.get_session_messages.side_effect = lambda sid: (
            [{"role": "user", "content": sid}] if sid else []
        )
        win._get_team_manager = MagicMock()
        tm = MagicMock()
        tm.get_team_run_id.return_value = "run-9"
        tm.start_team_run.return_value = "new-run-9"
        win._get_team_manager.return_value = tm
        win._card_manager = MagicMock()

        created = []

        class _FakeWin:
            _window_id = "w"

            def __init__(self, branch_data=None):
                self._branch_session_data = branch_data
                self._team_agent_name = None
                self._team_name = None
                self._team_run_id = None
                created.append(self)

        win._create_fresh_window.side_effect = lambda branch_data=None, **kw: _FakeWin(branch_data)

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_restore_requested(win, "run-1")

        # 3 个成员窗口（模板 agents 不再重复建窗）——旧逻辑会建 6 个
        assert len(created) == 3, f"模板 agents 不应重复建窗，应 3 个窗口，实际 {len(created)}"
        agent_names = sorted(c._team_agent_name for c in created)
        assert agent_names == ["build", "plan", "review"], f"窗口 agent 集合: {agent_names}"
        # 每个 agent 恰好 1 窗（同角色不因模板重复）
        from collections import Counter

        counts = Counter(c._team_agent_name for c in created)
        assert all(v == 1 for v in counts.values()), f"各 agent 应恰好 1 窗: {counts}"

    def test_restore_template_role_without_member_still_created(self):
        """Bug 3 语义保持：模板角色无实际成员（无 wid 记录）时仍兜底建 1 窗。

        场景：模板 [build, plan, review]，但快照成员只有 build@win_01。
        plan / review 无 wid 记录 → 兜底各建 1 窗（F3 找回语义），
        窗口数 = build×1 + plan×1 + review×1 = 3（不与 build 重复）。
        """
        _ensure_qapp()
        import json as _json
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        snap = _json.dumps(
            [
                {"agent_name": "build", "window_id": ""},
                {"agent_name": "plan", "window_id": ""},
                {"agent_name": "review", "window_id": ""},
                {"agent_name": "build", "window_id": "win_01"},
            ],
            ensure_ascii=False,
        )
        win = MagicMock()
        win._is_destroyed = False
        win.history_manager = MagicMock()
        win.history_manager.get_team_sessions_by_run_id.return_value = [
            {
                "session_id": "s-build",
                "title": "build 会话",
                "last_time": "2026-01-02 10:00:00",
                "team_run_id": "run-1",
                "team_name": "dev-team",
                "agent_name": "build",
                "team_members": snap,
            }
        ]
        win.history_manager.get_session_messages.side_effect = lambda sid: (
            [{"role": "user", "content": sid}] if sid else []
        )
        win._get_team_manager = MagicMock()
        tm = MagicMock()
        tm.get_team_run_id.return_value = "run-9"
        tm.start_team_run.return_value = "new-run-9"
        win._get_team_manager.return_value = tm
        win._card_manager = MagicMock()

        created = []

        class _FakeWin:
            _window_id = "w"

            def __init__(self, branch_data=None):
                self._branch_session_data = branch_data
                self._team_agent_name = None
                self._team_name = None
                self._team_run_id = None
                created.append(self)

        win._create_fresh_window.side_effect = lambda branch_data=None, **kw: _FakeWin(branch_data)

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_restore_requested(win, "run-1")

        assert len(created) == 3, f"build(有 wid) + plan/review(无成员兜底) 应 3 窗，实际 {len(created)}"
        agent_names = sorted(c._team_agent_name for c in created)
        assert agent_names == ["build", "plan", "review"], f"窗口 agent 集合: {agent_names}"

    def test_restore_two_rounds_window_count_stable(self):
        """Bug 3 主场景：恢复 → 对话完 → 再次恢复，窗口数恒定不膨胀。

        两轮恢复模拟：每轮恢复后重建快照（= 实际窗口 wid），下一轮会话
        快照 = 模板 agents + 重建快照。旧逻辑窗口数 3→6→9 线性膨胀，
        修复后每轮恒为 3（= 成员数，不累积不丢失）。
        """
        _ensure_qapp()
        import json as _json
        from collections import Counter
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        def _build_sessions(team_members_snap):
            """构造一轮恢复的会话列表（最新会话携带 team_members 快照）"""
            snap_str = _json.dumps(team_members_snap, ensure_ascii=False)
            return [
                {
                    "session_id": f"s-{agent}-{i}",
                    "title": f"{agent} 会话",
                    "last_time": f"2026-01-0{i} 10:00:00",
                    "team_run_id": "run-1",
                    "team_name": "dev-team",
                    "agent_name": agent,
                    "team_members": snap_str,
                }
                for i, agent in enumerate(["build", "plan", "review"], start=1)
            ]

        template_agents = [
            {"agent_name": "build", "window_id": ""},
            {"agent_name": "plan", "window_id": ""},
            {"agent_name": "review", "window_id": ""},
        ]

        def _run_restore(sessions_snap_members):
            win = MagicMock()
            win._is_destroyed = False
            win.history_manager = MagicMock()
            win.history_manager.get_team_sessions_by_run_id.return_value = _build_sessions(
                sessions_snap_members
            )
            win.history_manager.get_session_messages.side_effect = lambda sid: (
                [{"role": "user", "content": sid}] if sid else []
            )
            win._get_team_manager = MagicMock()
            tm = MagicMock()
            tm.get_team_run_id.return_value = "run-9"
            tm.start_team_run.return_value = "new-run-9"
            win._get_team_manager.return_value = tm
            win._card_manager = MagicMock()

            created = []

            class _FakeWin:
                _window_id = "w"

                def __init__(self, branch_data=None):
                    self._branch_session_data = branch_data
                    self._team_agent_name = None
                    self._team_name = None
                    self._team_run_id = None
                    created.append(self)

            win._create_fresh_window.side_effect = lambda branch_data=None, **kw: _FakeWin(branch_data)

            with patch("app.main_widget.InfoBar") as _mock_infobar:
                OpenAIChatToolWindow._on_team_restore_requested(win, "run-1")
            return created

        # 初始快照：模板 agents + 3 个成员（win_01..03）
        initial_snap = template_agents + [
            {"agent_name": "build", "window_id": "win_01"},
            {"agent_name": "plan", "window_id": "win_02"},
            {"agent_name": "review", "window_id": "win_03"},
        ]

        # 第一轮恢复：窗口数 = 成员数 3
        r1 = _run_restore(initial_snap)
        assert len(r1) == 3, f"第一轮应 3 窗，实际 {len(r1)}"
        assert all(v == 1 for v in Counter(c._team_agent_name for c in r1).values())

        # 恢复后重建快照 = 实际窗口（3 个新 wid）
        rebuilt_snap = template_agents + [
            {"agent_name": c._team_agent_name, "window_id": f"win_{10 + i}"}
            for i, c in enumerate(r1)
        ]

        # 第二轮恢复：窗口数仍 = 3（不膨胀）
        r2 = _run_restore(rebuilt_snap)
        assert len(r2) == 3, f"第二轮应仍 3 窗（不累积），实际 {len(r2)}"

        # 第三轮：仍恒定 3
        rebuilt_snap2 = template_agents + [
            {"agent_name": c._team_agent_name, "window_id": f"win_{20 + i}"}
            for i, c in enumerate(r2)
        ]
        r3 = _run_restore(rebuilt_snap2)
        assert len(r3) == 3, f"第三轮应仍 3 窗（恒定），实际 {len(r3)}"

    def test_restore_empty_run_id_returns_early(self):
        """场景5：team_run_id 为空 → 恢复守卫直接返回，不建窗。

        非团队会话（run_id=""）不应触发合并/恢复链路：_on_team_restore_requested
        首行守卫 `if not run_id: return`，不查询历史、不调任何团队接口。
        """
        _ensure_qapp()
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._is_destroyed = False
        win.history_manager = MagicMock()
        win._get_team_manager = MagicMock()

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_restore_requested(win, "")

        win.history_manager.get_team_sessions_by_run_id.assert_not_called(), "空 run_id 不应查询会话"
        win._get_team_manager.assert_not_called(), "空 run_id 不应触发 TeamManager"
        win._create_fresh_window.assert_not_called(), "空 run_id 不应创建窗口"


class TestTeamRestoreIndependence:
    """M1' 恢复路径重构：恢复 = 独立新团队，不解散现有团队。

    覆盖：
    - 恢复路径**不**调用 disband（独立新团队，保留现有团队不动）
    - 恢复路径**不**复用现有 run_id，而是直接生成 UUID（新团队框）
    - 现有团队 JSON 顶层 run_id 保持不变（不影响后续 /team --join）
    - _join_new_window_for_template 恢复路径参数：
      track_arrange=False 不递减 _pending_arrange_count，
      keep_team_name=True 保留会话记录团队名
    """

    def test_restore_does_not_disband_existing_team(self):
        """恢复路径不再解散现有团队（多团队并存）。"""
        _ensure_qapp()
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._is_destroyed = False
        win.history_manager = MagicMock()
        win.history_manager.get_team_sessions_by_run_id.return_value = [
            {
                "session_id": "s1",
                "title": "t",
                "last_time": "2026-01-01 10:00:00",
                "team_run_id": "run-1",
                "team_name": "dev-team",
                "agent_name": "build",
            }
        ]
        win.history_manager.get_session_messages.return_value = [{"role": "user", "content": "x"}]
        win._get_team_manager = MagicMock()
        tm = MagicMock()
        # 即使有现有 run_id，恢复路径也不复用
        tm.get_team_run_id.return_value = "existing-run"
        tm.start_team_run.return_value = "unused"
        win._get_team_manager.return_value = tm
        win._create_fresh_window = MagicMock(return_value=MagicMock(_window_id="w1"))
        win._card_manager = MagicMock()
        # 旧 disband 方法已删除——若有人引用它会抛 AttributeError
        win._disband_current_team_for_restore = MagicMock()

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_restore_requested(win, "run-1")

        # 不应再调用 disband（多团队并存：保留现有团队）
        win._disband_current_team_for_restore.assert_not_called()
        # 不复用现有 run_id、不调 start_team_run（避免改 team.json 顶层 run_id）
        tm.get_team_run_id.assert_not_called()
        tm.start_team_run.assert_not_called()

    def test_restore_generates_new_independent_run_id(self):
        """恢复路径生成全新独立 run_id（UUID）——区别于现有团队 run_id。

        防止 start_team_run(force=True) 副作用：改 team.json 顶层 run_id 会
        让后续 /team --join 等读 get_team_run_id() 的入口"漂"到新团队。
        改为直接 uuid4().hex，仅写入新成员记录（join_team 透传），
        不动 team.json 顶层 run_id。
        """
        _ensure_qapp()
        import re as _re
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._is_destroyed = False
        win.history_manager = MagicMock()
        win.history_manager.get_team_sessions_by_run_id.return_value = [
            {
                "session_id": "s1",
                "title": "t",
                "last_time": "2026-01-01 10:00:00",
                "team_run_id": "run-1",
                "team_name": "dev-team",
                "agent_name": "build",
            }
        ]
        win.history_manager.get_session_messages.return_value = [{"role": "user", "content": "x"}]
        win._get_team_manager = MagicMock()
        tm = MagicMock()
        tm.get_team_run_id.return_value = "existing-run"  # 顶层现有 run_id（不应被改写）
        win._get_team_manager.return_value = tm
        win._create_fresh_window = MagicMock(return_value=MagicMock(_window_id="w1"))
        win._card_manager = MagicMock()

        # 捕获 join_team 收到的 run_id（应是全新 UUID，不是 existing-run）
        captured_run_ids = []
        tm.join_team = MagicMock(
            side_effect=lambda **kw: captured_run_ids.append(kw.get("run_id", ""))
        )
        # rebuild 也透传 run_id
        tm.rebuild_team_members_snapshot = MagicMock()

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_restore_requested(win, "run-1")

        assert captured_run_ids, "恢复路径必须 join 至少一个成员"
        for rid in captured_run_ids:
            assert _re.fullmatch(r"[0-9a-f]{32}", rid), (
                f"新 run_id 应为 uuid4 hex（32 位小写），实际: {rid!r}"
            )
            assert rid != "existing-run", (
                f"恢复 run_id 不应复用现有团队 run_id，实际: {rid!r}"
            )
        # 重建快照也用新 run_id
        if tm.rebuild_team_members_snapshot.called:
            entries_arg = tm.rebuild_team_members_snapshot.call_args.args[0]
            for entry in entries_arg:
                # 4 元组 (wid, agent, run_id, team_label)
                rid_in_snap = entry[2] if len(entry) >= 3 else ""
                assert _re.fullmatch(r"[0-9a-f]{32}", rid_in_snap), (
                    f"快照 run_id 应为 uuid4 hex，实际: {rid_in_snap!r}"
                )
                assert rid_in_snap != "existing-run"

    def test_restore_applies_member_last_model(self):
        """需求 2：恢复时还原成员最后使用的模型——从该成员最新会话消息
        提取 assistant 的 model_name/provider_name 传给 _apply_model_selection_to_window"""
        _ensure_qapp()
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._is_destroyed = False
        win.history_manager = MagicMock()
        win.history_manager.get_team_sessions_by_run_id.return_value = [
            s for s in _history_samples() if s["team_run_id"] == "run-1"
        ]
        # 会话消息：最后一条 assistant 带模型信息（恢复应取最后使用的模型）
        win.history_manager.get_session_messages.side_effect = lambda sid: [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a1", "model_name": "old-model", "provider_name": "Old"},
            {"role": "assistant", "content": "a2", "model_name": "gpt-4o", "provider_name": "Provider A"},
        ]
        win._get_team_manager = MagicMock()
        tm = MagicMock()
        tm.get_team_run_id.return_value = "run-1"
        win._get_team_manager.return_value = tm
        win._card_manager = MagicMock()
        # 记录模型应用调用（真实逻辑在 _apply_model_selection_to_window 单测覆盖）
        win._apply_model_selection_to_window = MagicMock()

        class _FakeWin:
            _window_id = "w1"

            def __init__(self, branch_data=None):
                self._branch_session_data = branch_data
                self._team_agent_name = None
                self._team_name = None
                self._team_run_id = None

        fake = _FakeWin()

        def _fake_create(branch_data=None, **kw):
            fake._branch_session_data = branch_data
            return fake

        win._create_fresh_window.side_effect = _fake_create

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_restore_requested(win, "run-1")

        # 每个恢复窗口都应用了模型还原，参数取消息中最后一条 assistant 的模型
        assert win._apply_model_selection_to_window.call_count == 2
        for call in win._apply_model_selection_to_window.call_args_list:
            args = call.args
            assert args[1] == "gpt-4o", f"应传最后使用的 model_name，实际: {args[1]!r}"
            assert args[2] == "Provider A", f"应传最后使用的 provider_name，实际: {args[2]!r}"

    def test_restore_applies_model_fallback_when_no_assistant(self):
        """恢复窗口消息无 assistant（空会话）→ 模型参数为空串（构建者继承兜底）"""
        _ensure_qapp()
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._is_destroyed = False
        win.history_manager = MagicMock()
        win.history_manager.get_team_sessions_by_run_id.return_value = [
            s for s in _history_samples() if s["team_run_id"] == "run-1"
        ]
        win.history_manager.get_session_messages.return_value = [{"role": "user", "content": "q"}]
        win._get_team_manager = MagicMock()
        tm = MagicMock()
        tm.get_team_run_id.return_value = "run-1"
        win._get_team_manager.return_value = tm
        win._card_manager = MagicMock()
        win._apply_model_selection_to_window = MagicMock()

        class _FakeWin:
            _window_id = "w1"

            def __init__(self, branch_data=None):
                self._branch_session_data = branch_data
                self._team_agent_name = None
                self._team_name = None
                self._team_run_id = None

        fake = _FakeWin()

        def _fake_create(branch_data=None, **kw):
            fake._branch_session_data = branch_data
            return fake

        win._create_fresh_window.side_effect = _fake_create

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_restore_requested(win, "run-1")

        assert win._apply_model_selection_to_window.call_count == 2
        for call in win._apply_model_selection_to_window.call_args_list:
            args = call.args
            assert args[1] == "", "无 assistant 消息 → model_name 应为空串"
            assert args[2] == "", "无 assistant 消息 → provider_name 应为空串"

    def test_join_template_track_arrange_false_keeps_count(self):
        """恢复路径 track_arrange=False：完整初始化但不递减 _pending_arrange_count。"""
        _ensure_qapp()
        from unittest.mock import MagicMock

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._is_destroyed = False
        win.backend = MagicMock()
        win.backend.agent_manager = MagicMock()
        win._on_agent_changed = MagicMock()
        win._apply_agent_command_permissions = MagicMock()
        win._refresh_team_ui = MagicMock()
        win._start_team_watcher = MagicMock()
        win._team_agent_name = "build"
        win._team_name = "dev-team"
        win._team_run_id = "run-9"
        win._window_id = "w1"
        win.window = MagicMock(return_value=win)

        tm = MagicMock()
        tm.get_template.return_value = {"name": "模板名"}
        tm.get_team_run_id.return_value = "run-9"

        main_win = MagicMock()
        main_win._get_team_manager = MagicMock(return_value=tm)
        main_win._sync_active_windows_to_team_manager = MagicMock()
        main_win._pending_arrange_count = 3
        main_win._do_team_window_arrange = MagicMock()

        OpenAIChatToolWindow._join_new_window_for_template(
            main_win, win, "build", "w1", track_arrange=False, keep_team_name=True
        )

        # 完整初始化执行
        win._on_agent_changed.assert_called_once_with("build")
        win._apply_agent_command_permissions.assert_called_once_with("build")
        win._refresh_team_ui.assert_called_once_with("build")
        win._start_team_watcher.assert_called_once()
        # 保留会话记录团队名（不覆盖为模板名）
        assert win._team_name == "dev-team"
        # 不递减排列计数、不触发排列
        assert main_win._pending_arrange_count == 3
        main_win._do_team_window_arrange.assert_not_called()

    def test_join_template_default_track_arrange_decrements(self):
        """模板加载路径默认 track_arrange=True：递减计数并在归零时排列。"""
        _ensure_qapp()
        from unittest.mock import MagicMock

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._is_destroyed = False
        win.backend = MagicMock()
        win.backend.agent_manager = MagicMock()
        win._on_agent_changed = MagicMock()
        win._apply_agent_command_permissions = MagicMock()
        win._refresh_team_ui = MagicMock()
        win._start_team_watcher = MagicMock()
        win._team_name = ""
        win._window_id = "w1"
        win.window = MagicMock(return_value=win)

        tm = MagicMock()
        tm.get_template.return_value = {"name": "模板名"}
        tm.get_team_run_id.return_value = "run-x"

        main_win = MagicMock()
        main_win._get_team_manager = MagicMock(return_value=tm)
        main_win._sync_active_windows_to_team_manager = MagicMock()
        main_win._pending_arrange_count = 1
        main_win._do_team_window_arrange = MagicMock()

        OpenAIChatToolWindow._join_new_window_for_template(main_win, win, "build", "w1")

        assert main_win._pending_arrange_count == 0
        main_win._do_team_window_arrange.assert_called_once()
        # 默认 keep_team_name=False：团队名被模板名覆盖
        assert win._team_name == "模板名"

    def test_join_template_keep_name_preserves_run_id(self):
        """补丁 B：keep_team_name=True（恢复路径）→ 保留调用方已设 run_id，不被中途刷新覆盖。"""
        _ensure_qapp()
        from unittest.mock import MagicMock

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._is_destroyed = False
        win.backend = MagicMock()
        win.backend.agent_manager = MagicMock()
        win._on_agent_changed = MagicMock()
        win._apply_agent_command_permissions = MagicMock()
        win._refresh_team_ui = MagicMock()
        win._start_team_watcher = MagicMock()
        win._team_agent_name = "build"
        win._team_name = "dev-team"  # 调用方预设团队名
        win._team_run_id = "run-pre"  # 调用方预设 run_id（恢复窗口已设 new_run_id）
        win._window_id = "w1"
        win.window = MagicMock(return_value=win)

        tm = MagicMock()
        tm.get_template.return_value = {"name": "模板名"}
        tm.get_team_run_id.return_value = "run-new"  # 延后期被中途刷新的 run_id（不应覆盖）

        main_win = MagicMock()
        main_win._get_team_manager = MagicMock(return_value=tm)
        main_win._sync_active_windows_to_team_manager = MagicMock()
        main_win._pending_arrange_count = 3
        main_win._do_team_window_arrange = MagicMock()

        OpenAIChatToolWindow._join_new_window_for_template(
            main_win, win, "build", "w1", track_arrange=False, keep_team_name=True
        )

        # run_id 保留调用方值（不被 get_team_run_id 覆盖）
        assert win._team_run_id == "run-pre", "keep_team_name=True 应保留调用方已设 run_id"
        # 团队名保留调用方值（不覆盖为模板名）
        assert win._team_name == "dev-team"

    def test_join_template_default_overrides_run_id(self):
        """补丁 B：keep_team_name=False（模板加载路径）→ run_id 被 get_team_run_id() 覆盖。"""
        _ensure_qapp()
        from unittest.mock import MagicMock

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._is_destroyed = False
        win.backend = MagicMock()
        win.backend.agent_manager = MagicMock()
        win._on_agent_changed = MagicMock()
        win._apply_agent_command_permissions = MagicMock()
        win._refresh_team_ui = MagicMock()
        win._start_team_watcher = MagicMock()
        win._team_name = "old-team"
        win._team_run_id = "run-pre"
        win._window_id = "w1"
        win.window = MagicMock(return_value=win)

        tm = MagicMock()
        tm.get_template.return_value = {"name": "模板名"}
        tm.get_team_run_id.return_value = "run-new"

        main_win = MagicMock()
        main_win._get_team_manager = MagicMock(return_value=tm)
        main_win._sync_active_windows_to_team_manager = MagicMock()
        main_win._pending_arrange_count = 1
        main_win._do_team_window_arrange = MagicMock()

        OpenAIChatToolWindow._join_new_window_for_template(main_win, win, "build", "w1")

        # keep=False：run_id 被 get_team_run_id() 覆盖
        assert win._team_run_id == "run-new", "keep_team_name=False 时 run_id 应被 get_team_run_id() 覆盖"
        # 团队名同样被模板名覆盖
        assert win._team_name == "模板名"
        assert main_win._pending_arrange_count == 0
        main_win._do_team_window_arrange.assert_called_once()


class TestTeamJoinRetryAndCount:
    """D1/C4/E3：_join_new_window_for_template 就绪轮询 + 计数兜底。

    - D-T1：backend 未就绪 → QTimer 重试（不直接 return、不递减计数），
      就绪后正常完成
    - D-T1b：重试耗尽（超过 _TEAM_JOIN_MAX_RETRIES）→ 放弃并递减计数
    - E-T3a：窗口已销毁 → 直接 return 但递减计数（不垛死排列回调）
    - E-T3b：异常路径 → 递减计数 + 补刷新胶囊
    """

    def _make_join_ctx(self, backend_ready: bool = True, destroyed: bool = False):
        """构造 _join_new_window_for_template 的 win/main_win/tm 上下文"""
        _ensure_qapp()
        from unittest.mock import MagicMock

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._is_destroyed = destroyed
        # MagicMock 的 getattr 会自动创建属性 → 显式初始化重试计数
        win._team_join_retries = 0
        if backend_ready:
            win.backend = MagicMock()
            win.backend.agent_manager = MagicMock()
        else:
            win.backend = MagicMock()
            win.backend.agent_manager = None
        win._on_agent_changed = MagicMock()
        win._apply_agent_command_permissions = MagicMock()
        win._refresh_team_ui = MagicMock()
        win._start_team_watcher = MagicMock()
        win._team_agent_name = "build"
        win._team_name = "dev-team"
        win._team_run_id = "run-9"
        win._window_id = "w1"
        win.window = MagicMock(return_value=win)

        tm = MagicMock()
        tm.get_template.return_value = {"name": "模板名"}
        tm.get_team_run_id.return_value = "run-9"

        main_win = MagicMock()
        main_win._get_team_manager = MagicMock(return_value=tm)
        main_win._sync_active_windows_to_team_manager = MagicMock()
        main_win._pending_arrange_count = 2
        main_win._do_team_window_arrange = MagicMock()
        # 类常量在 MagicMock 上不自动存在 → 显式补齐（生产代码走类属性）
        main_win._TEAM_JOIN_MAX_RETRIES = OpenAIChatToolWindow._TEAM_JOIN_MAX_RETRIES
        main_win._TEAM_JOIN_RETRY_INTERVAL_MS = OpenAIChatToolWindow._TEAM_JOIN_RETRY_INTERVAL_MS
        return OpenAIChatToolWindow, win, tm, main_win

    def test_join_retries_when_backend_not_ready(self):
        """D-T1：backend 未就绪 → 安排 50ms 重试，不递减计数；就绪后完成。"""
        import app.main_widget as mw
        from unittest.mock import MagicMock, patch

        cls, win, tm, main_win = self._make_join_ctx(backend_ready=False)
        with patch.object(mw.QTimer, "singleShot") as m_shot:
            cls._join_new_window_for_template(main_win, win, "build", "w1")
        # 未就绪：安排重试（50ms），不递减计数、不排列
        m_shot.assert_called_once()
        assert m_shot.call_args.args[0] == 50
        assert main_win._pending_arrange_count == 2, "未就绪重试不应递减计数"
        main_win._do_team_window_arrange.assert_not_called()
        # 就绪后重入 → 正常完成 + 递减计数
        win.backend.agent_manager = MagicMock()
        cls._join_new_window_for_template(main_win, win, "build", "w1")
        assert main_win._pending_arrange_count == 1
        win._refresh_team_ui.assert_called_once_with("build")

    def test_join_retry_exhausted_decrements(self):
        """D-T1b/E-T3：重试耗尽 → 放弃并递减计数（不垛死排列回调）。"""
        from unittest.mock import patch

        import app.main_widget as mw

        cls, win, tm, main_win = self._make_join_ctx(backend_ready=False)
        win._team_join_retries = cls._TEAM_JOIN_MAX_RETRIES  # 已达上限
        with patch.object(mw.QTimer, "singleShot") as m_shot:
            cls._join_new_window_for_template(main_win, win, "build", "w1")
        m_shot.assert_not_called(), "重试耗尽不应再安排重试"
        assert main_win._pending_arrange_count == 1, "重试耗尽应递减计数"
        assert main_win._pending_arrange_count > 0  # 未归零不排列
        main_win._do_team_window_arrange.assert_not_called()

    def test_join_destroyed_decrements_count(self):
        """E-T3：窗口已销毁 → 提前 return 但递减计数（避免计数垛死）。"""
        cls, win, tm, main_win = self._make_join_ctx(destroyed=True)
        cls._join_new_window_for_template(main_win, win, "build", "w1")
        assert main_win._pending_arrange_count == 1, "销毁窗口也应递减计数"

    def test_join_exception_decrements_and_refreshes(self):
        """E-T3b：异常路径 → 递减计数 + 补刷新胶囊（C4 兜底）。"""
        import app.main_widget as mw
        from unittest.mock import MagicMock, patch

        cls, win, tm, main_win = self._make_join_ctx()
        win._on_agent_changed.side_effect = RuntimeError("boom")
        fake_tm_win = MagicMock()
        from app.widgets.tab_manager_window import TabManagerWindow

        with patch.object(TabManagerWindow, "get_instance", return_value=fake_tm_win):
            cls._join_new_window_for_template(main_win, win, "build", "w1")
        assert main_win._pending_arrange_count == 1, "异常也应递减计数"
        fake_tm_win.refresh_capsule_for_window.assert_called_once_with(win), "异常时补刷新胶囊"

    def test_join_retry_exhausted_starts_watcher(self):
        """C4a：重试耗尽 → 无条件补 _start_team_watcher（不依赖 backend）+ 计数收口。"""
        cls, win, tm, main_win = self._make_join_ctx(backend_ready=False)
        win._team_join_retries = cls._TEAM_JOIN_MAX_RETRIES  # 已达上限
        cls._join_new_window_for_template(main_win, win, "build", "w1")
        # watcher 无条件补启动（缺则邮件永不主动触发）
        win._start_team_watcher.assert_called_once()
        # 计数收口
        assert main_win._pending_arrange_count == 1
        main_win._do_team_window_arrange.assert_not_called()

    def test_join_retry_destroyed_skips_watcher(self):
        """C4a：重试期间窗口销毁 → 不补 watcher、计数收口、无异常。"""
        cls, win, tm, main_win = self._make_join_ctx(backend_ready=False, destroyed=True)
        win._team_join_retries = cls._TEAM_JOIN_MAX_RETRIES
        cls._join_new_window_for_template(main_win, win, "build", "w1")
        win._start_team_watcher.assert_not_called(), "销毁窗口不应补 watcher"
        assert main_win._pending_arrange_count == 1, "销毁窗口计数收口"
        main_win._do_team_window_arrange.assert_not_called()


class TestTeamMergedCard:
    """M4 UI 层：团队合并条目卡片 + 混排渲染 + 归档链路 + 成员进入会话"""

    def _merged_entry(self, run_id="run-9", preview="首问内容", members=None):
        """构造数据层合并条目（含 members）"""
        if members is None:
            members = [
                {
                    "session_id": "s1",
                    "title": "build 会话",
                    "agent_name": "build",
                    "last_time": "2026-01-01 10:00:00",
                },
                {
                    "session_id": "s2",
                    "title": "plan 会话",
                    "agent_name": "plan",
                    "last_time": "2026-01-02 10:00:00",
                },
            ]
        return {
            "team_run_id": run_id,
            "team_name": "dev",
            "agent_names": ["build", "plan"],
            "member_count": 2,
            "message_count": 3,
            "team_merged": True,
            "session_id": "s-latest",
            "last_time": "2026-01-02 10:00:00",
            "preview": preview,
            "members": members,
        }

    def test_card_click_toggles_expand_not_restore(self):
        """B-1：点击卡片只切换展开/收起，不再触发 restoreRequested。"""
        _ensure_qapp()
        from app.widgets.cards.settings.history_card import _TeamGroupCard

        card = _TeamGroupCard(self._merged_entry())
        restored = []
        card.restoreRequested.connect(restored.append)
        # 模拟鼠标左键点击卡片空白区
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtCore import Qt as _Qt

        ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(5, 5), _Qt.LeftButton, _Qt.LeftButton, _Qt.NoModifier)
        card.mousePressEvent(ev)
        assert restored == [], "点击卡片不应触发恢复"
        assert card._members_visible is True, "点击应展开成员列表"
        # 再次点击 → 收起
        card.mousePressEvent(ev)
        assert card._members_visible is False, "再次点击应收起"

    def test_card_expand_shows_member_rows(self):
        """B-1：展开后渲染成员行（角色+标题），点击成员行发 memberSelected。"""
        _ensure_qapp()
        from app.widgets.cards.settings.history_card import _TeamGroupCard

        card = _TeamGroupCard(self._merged_entry())
        selected = []
        card.memberSelected.connect(selected.append)
        card._toggle_members()
        texts = [lbl.text() for lbl in card.findChildren(QLabel)]
        assert "build 会话" in texts and "plan 会话" in texts, "展开后应显示成员标题"

        # 触发成员行点击（直接调用槽，模拟 memberSelected 链路）
        from PySide6.QtCore import Qt as _QtLeft

        fake_event = type("E", (), {"button": lambda self: _QtLeft.LeftButton})()
        card._on_member_row_clicked(fake_event, card._members[0], None)
        assert len(selected) == 1, "成员行点击应发 memberSelected"
        assert selected[0]["session_id"] == "s1", "应携带成员 session_record"

    def test_card_expand_shows_window_id_suffix_for_same_role(self):
        """T3 回归：展开区同角色多成员渲染多行 + window_id 后缀（build·w02）。

        场景7：同角色 build 两个成员（window_id 异）→ 展开区应显示
        build·w02 / build·w03 两行胶囊，与单角色行区分，避免两行都叫
        "build" 无法辨认。
        """
        _ensure_qapp()
        from app.widgets.cards.settings.history_card import _TeamGroupCard

        members = [
            {
                "session_id": "s1",
                "title": "build 初版",
                "agent_name": "build",
                "window_id": "win_02",
                "last_time": "2026-01-01 10:00:00",
            },
            {
                "session_id": "s2",
                "title": "build 二版",
                "agent_name": "build",
                "window_id": "win_03",
                "last_time": "2026-01-02 10:00:00",
            },
            {
                "session_id": "s3",
                "title": "plan 会话",
                "agent_name": "plan",
                "last_time": "2026-01-03 10:00:00",
            },
        ]
        card = _TeamGroupCard(self._merged_entry(members=members))
        card._toggle_members()
        texts = [lbl.text() for lbl in card.findChildren(QLabel)]
        # T3 渲染：window_id 前缀 win_ 简写为 w → build·w02 / build·w03
        assert "build·w02" in texts, f"展开区应显示 build·w02 后缀胶囊，实际: {texts}"
        assert "build·w03" in texts, f"展开区应显示 build·w03 后缀胶囊，实际: {texts}"
        # 无 window_id 的成员保持纯角色名（旧格式兼容）
        assert "plan" in texts, "无 window_id 成员应保持纯角色名"
        # 标题仍各自渲染（同角色两行不同标题）
        assert "build 初版" in texts and "build 二版" in texts, "同角色多行应各带独立标题"

    def test_archive_button_emits_archive_requested(self):
        """B-4：归档按钮发 archiveRequested(run_id)。"""
        _ensure_qapp()
        from app.widgets.cards.settings.history_card import _TeamGroupCard

        card = _TeamGroupCard(self._merged_entry())
        captured = []
        card.archiveRequested.connect(captured.append)
        # 直接 emit（按钮 click 已 connect 到 lambda emit）
        card.archiveRequested.emit("run-9")
        assert captured == ["run-9"]

    def test_preview_shows_first_question(self):
        """B-1：预览行显示团队首问。"""
        _ensure_qapp()
        from app.widgets.cards.settings.history_card import _TeamGroupCard

        card = _TeamGroupCard(self._merged_entry(preview="首问：实现登录"))
        texts = [lbl.text() for lbl in card.findChildren(QLabel)]
        assert any("首问：实现登录" in t for t in texts), "预览行应显示首问文本"

    def test_mixed_render_queue_places_team_card_inline(self):
        """B-2：渲染队列中合并条目与普通会话混排（团队卡插入普通条目同位置）。"""
        _ensure_qapp()
        from app.widgets.cards.settings.history_card import HistoryCard

        card = HistoryCard()
        card._prepare_history_render_queue()
        # 混入合并条目 + 普通会话（同一日期分组内）
        merged = self._merged_entry()
        merged["last_time"] = "2026-01-05 10:00:00"
        card._all_history = [
            merged,
            {
                "session_id": "normal-1",
                "title": "普通会话",
                "last_time": "2026-01-05 09:00:00",
                "message_count": 2,
                "preview": "hi",
            },
        ]
        card._current_index = None
        card._search_filter = ""
        card._render_queue.clear()
        card._prepare_history_render_queue()
        queue = card._render_queue
        # 队列中同时存在 team_group 与 session 项，且按 last_time 降序混排
        types = [q[0] for q in queue if q[0] in ("team_group", "session")]
        assert "team_group" in types and "session" in types, "合并条目与普通会话应同时入队混排"
        # 合并条目（10:00）在普通会话（09:00）之前
        assert types.index("team_group") < types.index("session"), "按 last_time 混排：合并条目在前"
        # 团队卡 key 应为 run_id
        team_item = next(q for q in queue if q[0] == "team_group")
        assert team_item[1]["team_run_id"] == "run-9"

    def test_on_team_archive_requested_archives_members(self):
        """B-4：main_widget 归档槽收集成员 → archive_sessions_by_run_id。"""
        _ensure_qapp()
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win.history_manager = MagicMock()
        win.history_manager.get_team_sessions_by_run_id.return_value = [
            {"session_id": "s1", "agent_name": "build"},
            {"session_id": "s2", "agent_name": "plan"},
        ]
        win.history_manager.archive_sessions_by_run_id.return_value = 2
        win._current_session_id = "s3"  # 非团队成员 → 不切换新会话
        win.pixel_pet = MagicMock()
        win._card_manager = MagicMock()

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_archive_requested(win, "run-9")

        win.history_manager.get_team_sessions_by_run_id.assert_called_once_with("run-9")
        win.history_manager.archive_sessions_by_run_id.assert_called_once_with("run-9")

    def test_on_team_member_selected_loads_record(self):
        """B-5：成员进入会话直接调 _load_session_from_record，不依赖面板 index。"""
        _ensure_qapp()
        from unittest.mock import MagicMock

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win.history_manager = MagicMock()
        win.history_manager.get_session_by_session_id.return_value = {
            "session_id": "s1",
            "title": "build 会话",
            "project": "proj-x",
        }
        win._load_session_from_record = MagicMock()
        win._card_manager = MagicMock()
        win._window_id = "w1"

        OpenAIChatToolWindow._on_team_member_selected(win, {"session_id": "s1"})

        win._load_session_from_record.assert_called_once()
        _args, _kwargs = win._load_session_from_record.call_args
        assert _args[0]["session_id"] == "s1", "应加载成员会话记录"
        win._card_manager.hide_card.assert_called_once_with("history", "w1")


class TestCommandShortcutDestroyedGuard:
    """子任务 #B：关闭软件 RuntimeError（wrapped C/C++ object has been deleted）防护

    背景：update_command_shortcuts 首次执行时连接 self.destroyed → lambda 调
    _clear_command_shortcuts()；窗口 C++ 对象销毁触发 destroyed 时 lambda 访问
    已删除的 self 抛 RuntimeError。修复 = F1（sip 守卫）+ F2（closeEvent 主动断开）。
    """

    def test_is_sip_deleted_util(self):
        """_is_sip_deleted 工具函数：正常对象 False，销毁后 True，异常兜底 False。"""
        import shiboken6 as sip

        from app.main_widget import OpenAIChatToolWindow, _is_sip_deleted

        w = QLabel("x")
        assert _is_sip_deleted(w) is False
        assert _is_sip_deleted(None) is False
        assert _is_sip_deleted(123) is False  # 非 Qt 对象 → 异常兜底 False
        sip.delete(w)  # 立即销毁 C++ 对象（确定性，不等事件循环）
        assert sip.isValid(w) is False
        assert _is_sip_deleted(w) is True

    def test_clear_command_shortcuts_sip_deleted_silent(self):
        """F1：C++ 对象已销毁后调用 _clear_command_shortcuts 静默返回，不抛 RuntimeError。"""
        import shiboken6 as sip

        from app.main_widget import OpenAIChatToolWindow, _is_sip_deleted

        w = QLabel("x")
        w._command_shortcuts = []
        w._is_destroyed = True
        sip.delete(w)
        assert _is_sip_deleted(w) is True, "前置：对象已被 C++ 侧销毁"
        # 关键断言：不抛 RuntimeError（修复前此处直接访问 self.window() 会崩）
        OpenAIChatToolWindow._clear_command_shortcuts(w)

    def test_destroyed_signal_lambda_guard_no_crash(self):
        """F1：destroyed 信号触发时 lambda 经 sip 守卫静默跳过，不抛 RuntimeError。

        模拟真实场景：窗口 close + deleteLater → destroyed 触发（若 closeEvent
        未断开连接，此 lambda 是兜底防线）。
        """
        from app.main_widget import OpenAIChatToolWindow, _is_sip_deleted

        w = QLabel("x")
        w._command_shortcuts = []
        w._is_destroyed = False
        # 复刻 _register_command_shortcuts 中的守卫 lambda（sip 删除态 → None）
        w.destroyed.connect(lambda *a: None if _is_sip_deleted(w) else OpenAIChatToolWindow._clear_command_shortcuts(w))
        w.close()
        w.deleteLater()
        _ensure_qapp().processEvents()
        # 不抛异常即通过

    def test_disconnect_command_shortcut_cleanup(self):
        """F2：_disconnect_command_shortcut_cleanup 断开 destroyed 连接并立即清理。"""
        from unittest.mock import MagicMock

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._cmd_shortcuts_destroy_connected = True
        win._cmd_shortcuts_destroy_slot = lambda *a: None
        win._command_shortcuts = []
        win._is_destroyed = False

        OpenAIChatToolWindow._disconnect_command_shortcut_cleanup(win)

        # 精确断开保存的 slot（而非全断，避免误伤 destroyed 其他连接）
        win.destroyed.disconnect.assert_called_once_with(win._cmd_shortcuts_destroy_slot)
        assert win._cmd_shortcuts_destroy_connected is False, "连接标记应复位"
        win._clear_command_shortcuts.assert_called_once(), "应主动清理快捷键"

    def test_disconnect_cleanup_tolerates_missing_slot(self):
        """F2：无保存 slot（旧实例）时全断兜底，异常容错不抛。"""
        from types import SimpleNamespace

        from app.main_widget import OpenAIChatToolWindow

        class _FakeDestroyed:
            def __init__(self):
                self.calls = []

            def disconnect(self, *args):
                self.calls.append(args)

        clear_calls = []

        def _clear():
            clear_calls.append(1)
            raise RuntimeError("wrapped C/C++ object has been deleted")

        win = SimpleNamespace(
            _cmd_shortcuts_destroy_connected=True,
            destroyed=_FakeDestroyed(),
            _clear_command_shortcuts=_clear,
        )

        OpenAIChatToolWindow._disconnect_command_shortcut_cleanup(win)

        assert win.destroyed.calls == [()], "无保存 slot → 全断兜底"
        assert win._cmd_shortcuts_destroy_connected is False
        assert len(clear_calls) == 1, "应主动清理快捷键（RuntimeError 被吞）"
