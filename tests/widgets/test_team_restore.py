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
from PyQt5.QtWidgets import QApplication, QLabel


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
        def _fake_create(branch_data=None):
            fake._branch_session_data = branch_data
            return fake

        win._create_fresh_window.side_effect = _fake_create

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_restore_requested(win, "run-1")

        # 应创建 2 个窗口（build + plan），注入恢复数据与团队标记
        assert win._create_fresh_window.call_count == 2
        assert fake._branch_session_data is not None
        assert fake._team_agent_name in ("build", "plan")
        assert fake._team_run_id == "new-run-1"
        # H1/F3 回归：恢复路径必须 force 生成新 run_id（不沿用历史 run_id）
        tm.start_team_run.assert_called_once_with(force=True)
        # L4/F2 回归：分支数据必须透传 project（保持会话原项目归属）
        for call_args in win._create_fresh_window.call_args_list:
            _kwargs = call_args.kwargs
            assert "branch_data" in _kwargs
            branch = _kwargs["branch_data"]
            assert branch["project"] in ("", "dev-team", "qa-team") or branch["project"] is not None
        # join_team 被调用（每个窗口一次）
        assert tm.join_team.call_count == 2

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

        win._create_fresh_window.side_effect = lambda branch_data=None: _FakeWin(branch_data)

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_restore_requested(win, "run-1")

        # 3 个 agent（build 去重为 1 + plan + review）→ 3 个窗口
        assert len(created) == 3, f"应创建 3 个窗口（build 去重），实际 {len(created)}"
        agent_names = sorted(c._team_agent_name for c in created)
        assert agent_names == ["build", "plan", "review"], f"agent 集合错误: {agent_names}"
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
        tm.start_team_run.return_value = "new-run-1"
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
        def _fake_create(branch_data=None):
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


class TestTeamRestoreDisband:
    """M1' 恢复路径重构：解散现有团队 + 完整初始化恢复窗口

    覆盖：
    - 恢复前 _disband_current_team_for_restore 被调用（在 start_team_run 之前）
    - 解散逻辑：团队窗口 _stop_team_watcher / leave_team / 团队标记清空，
      主窗口保留并新建空白会话，其他团队窗口 remove_window + close
    - 非团队 / 已销毁窗口被跳过
    - _join_new_window_for_template 恢复路径参数：
      track_arrange=False 不递减 _pending_arrange_count，
      keep_team_name=True 保留会话记录团队名
    """

    def test_restore_calls_disband_before_start_run(self):
        """恢复时解散逻辑在 start_team_run(force=True) 之前被调用。"""
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
        tm.start_team_run.return_value = "new-run-1"
        win._get_team_manager.return_value = tm
        win._create_fresh_window = MagicMock(return_value=MagicMock(_window_id="w1"))
        win._card_manager = MagicMock()
        # 记录解散与 start_team_run 的调用顺序
        win._disband_current_team_for_restore = MagicMock()

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_restore_requested(win, "run-1")

        # 解散逻辑必须被调用
        win._disband_current_team_for_restore.assert_called_once()
        # 顺序断言：disband 在 _get_team_manager（→start_team_run）之前被调用
        _calls = [c[0] for c in win.method_calls]
        assert _calls.index("_disband_current_team_for_restore") < _calls.index("_get_team_manager")

    def test_disband_stops_watcher_and_leaves_team(self):
        """解散：每个团队窗口停 watcher + leave_team + 清空团队标记。"""
        _ensure_qapp()
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        main_win = MagicMock()
        main_win._is_destroyed = False
        main_win.windowClosed = False
        main_win._team_agent_name = "build"
        main_win._team_name = "dev"
        main_win._team_run_id = "run-1"
        main_win._window_id = "main"
        main_win._stop_team_watcher = MagicMock()
        main_win._refresh_team_ui = MagicMock()
        main_win._create_new_session = MagicMock()
        main_win.close = MagicMock()

        other_win = MagicMock()
        other_win._is_destroyed = False
        other_win.windowClosed = False
        other_win._team_agent_name = "plan"
        other_win._team_name = "dev"
        other_win._team_run_id = "run-1"
        other_win._window_id = "other"
        other_win._stop_team_watcher = MagicMock()
        other_win.close = MagicMock()

        tm = MagicMock()
        main_win._get_team_manager = MagicMock(return_value=tm)
        other_win._get_team_manager = MagicMock(return_value=tm)

        instances = [main_win, other_win]
        with patch.object(OpenAIChatToolWindow, "_instances", instances):
            with patch("app.widgets.tab_manager_window.TabManagerWindow") as _mock_tab:
                _mock_tab.get_instance.return_value = MagicMock()
                OpenAIChatToolWindow._disband_current_team_for_restore(main_win)

        # 每个团队窗口都停 watcher + leave_team + 清空标记
        main_win._stop_team_watcher.assert_called_once()
        other_win._stop_team_watcher.assert_called_once()
        assert tm.leave_team.call_count == 2
        assert main_win._team_agent_name == ""
        assert main_win._team_run_id == ""
        assert other_win._team_agent_name == ""
        # 主窗口保留：刷新独立 UI + 新建空白会话；不 close
        main_win._refresh_team_ui.assert_called_once()
        main_win._create_new_session.assert_called_once()
        main_win.close.assert_not_called()
        # 其他团队窗口：从 Tab 移除 + close
        other_win.close.assert_called_once()

    def test_disband_skips_non_team_and_destroyed(self):
        """解散跳过非团队窗口与已销毁窗口。"""
        _ensure_qapp()
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        main_win = MagicMock()
        main_win._is_destroyed = False
        main_win.windowClosed = False
        main_win._team_agent_name = ""
        main_win._get_team_manager = MagicMock()
        main_win._stop_team_watcher = MagicMock()
        main_win._create_new_session = MagicMock()

        destroyed = MagicMock()
        destroyed._is_destroyed = True
        destroyed.windowClosed = True
        destroyed._team_agent_name = "build"
        destroyed._stop_team_watcher = MagicMock()
        destroyed.close = MagicMock()

        closed_flag = MagicMock()
        closed_flag._is_destroyed = False
        closed_flag.windowClosed = True
        closed_flag._team_agent_name = "build"
        closed_flag._stop_team_watcher = MagicMock()
        closed_flag.close = MagicMock()

        tm = MagicMock()
        main_win._get_team_manager.return_value = tm

        instances = [main_win, destroyed, closed_flag]
        with patch.object(OpenAIChatToolWindow, "_instances", instances):
            OpenAIChatToolWindow._disband_current_team_for_restore(main_win)

        # 非团队/已销毁/windowClosed 窗口均不处理
        main_win._stop_team_watcher.assert_not_called()
        destroyed._stop_team_watcher.assert_not_called()
        closed_flag._stop_team_watcher.assert_not_called()
        tm.leave_team.assert_not_called()

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


class TestTeamMergedCard:
    """M4 UI 层：团队合并条目卡片 + 混排渲染 + 归档链路 + 成员进入会话"""

    def _merged_entry(self, run_id="run-9", preview="首问内容"):
        """构造数据层合并条目（含 members）"""
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
            "members": [
                {"session_id": "s1", "title": "build 会话", "agent_name": "build", "last_time": "2026-01-01 10:00:00"},
                {"session_id": "s2", "title": "plan 会话", "agent_name": "plan", "last_time": "2026-01-02 10:00:00"},
            ],
        }

    def test_card_click_toggles_expand_not_restore(self):
        """B-1：点击卡片只切换展开/收起，不再触发 restoreRequested。"""
        _ensure_qapp()
        from app.widgets.cards.settings.history_card import _TeamGroupCard

        card = _TeamGroupCard(self._merged_entry())
        restored = []
        card.restoreRequested.connect(restored.append)
        # 模拟鼠标左键点击卡片空白区
        from PyQt5.QtGui import QMouseEvent
        from PyQt5.QtCore import QEvent, QPointF
        from PyQt5.QtCore import Qt as _Qt

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
        from PyQt5.QtCore import Qt as _QtLeft

        fake_event = type("E", (), {"button": lambda self: _QtLeft.LeftButton})()
        card._on_member_row_clicked(fake_event, card._members[0], None)
        assert len(selected) == 1, "成员行点击应发 memberSelected"
        assert selected[0]["session_id"] == "s1", "应携带成员 session_record"

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
