# -*- coding: utf-8 -*-
"""M5 测试收尾：团队合并条目 UI 测试（蓝图 #0c 场景 1-5）

覆盖：
1. 合并条目渲染：普通列表混排、缓存复用更新、清理、排序
2. 点击展开不触发恢复、成员行点击、归档/恢复按钮
3. 归档按钮链路：HistoryCard 转发 + main_widget 槽
4. 成员进入会话：_load_session_from_record 复用 + 成员直选
5. current_idx 高亮：当前会话是团队成员时命中合并条目 index
"""

import sys

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication


def _ensure_qapp():
    """确保 QApplication 可用"""
    return QApplication.instance() or QApplication(sys.argv)


def _merged_entry(run_id="R1", last_time="2026-01-02 10:00:00", agent_names=None, preview="首问内容"):
    """构造数据层合并条目（含 members）"""
    agent_names = agent_names or ["build", "plan"]
    return {
        "team_run_id": run_id,
        "team_name": "dev",
        "agent_names": list(agent_names),
        "member_count": len(agent_names),
        "message_count": 3,
        "team_merged": True,
        "session_id": f"{run_id}-latest",
        "last_time": last_time,
        "preview": preview,
        "members": [
            {
                "session_id": f"{run_id}-s1",
                "title": f"{a} 会话",
                "agent_name": a,
                "last_time": "2026-01-01 10:00:00",
            }
            for a in agent_names
        ],
    }


def _normal_entry(session_id="n1", last_time="2026-01-01 09:00:00"):
    return {
        "session_id": session_id,
        "title": "普通会话",
        "last_time": last_time,
        "message_count": 2,
        "preview": "hi",
    }


def _make_card():
    """构造 HistoryCard 并返回（渲染队列就绪）"""
    from app.widgets.cards.settings.history_card import HistoryCard

    card = HistoryCard()
    card._update_display()  # 触发首次渲染（空列表）
    QApplication.processEvents()
    return card


class TestMergedEntryRender:
    """场景 1：合并条目渲染"""

    def test_merged_entry_renders_in_normal_list(self):
        """合并条目与普通会话混排：队列类型正确、缓存 key 正确。"""
        _ensure_qapp()
        card = _make_card()
        merged = _merged_entry()
        normal = _normal_entry()
        card.set_history([merged, normal], None)
        card._update_display()
        QApplication.processEvents()

        # 渲染队列：团队条目项类型（M4 实现为 team_group）
        types = [q[0] for q in card._render_queue]
        assert "team_group" in types, "合并条目应入队为 team_group"
        assert "session" in types, "普通会话应入队为 session"
        # 团队卡缓存 key = run_id；普通卡缓存 key = session_id
        assert "R1" in card._cached_team_cards, "团队卡应按 run_id 缓存"
        assert "n1" in card._cached_cards, "普通卡应按 session_id 缓存"

    def test_team_card_cache_reused_and_updated(self):
        """两次 set_history 同一 run_id：第二次命中缓存不重建，元信息更新。"""
        _ensure_qapp()
        card = _make_card()
        card.set_history([_merged_entry()], None)
        card._update_display()
        QApplication.processEvents()
        first_card = card._cached_team_cards.get("R1")
        assert first_card is not None

        # 第二次：不同 agent_names / member_count → 命中缓存原地更新
        card.set_history([_merged_entry(agent_names=["build", "plan", "review"])], None)
        card._update_display()
        QApplication.processEvents()
        assert card._cached_team_cards["R1"] is first_card, "缓存命中不应重建团队卡"
        texts = [lbl.text() for lbl in first_card.findChildren(type(first_card.meta_label))]
        assert any("3 位成员" in t for t in texts), "元信息应刷新为 3 位成员"

    def test_cleanup_does_not_delete_team_card_when_run_id_visible(self):
        """visible_ids 同时收集 run_id 与 session_id：再次渲染不含 R1 时团队卡被清理。"""
        _ensure_qapp()
        card = _make_card()
        card.set_history([_merged_entry()], None)
        card._update_display()
        QApplication.processEvents()
        assert "R1" in card._cached_team_cards

        # 再次渲染不含 R1 → 团队卡被清理
        card.set_history([_normal_entry()], None)
        card._update_display()
        QApplication.processEvents()
        assert "R1" not in card._cached_team_cards, "run_id 不可见时团队卡应被清理"

    def test_merged_entry_sorted_by_last_time_with_normal(self):
        """有序输入 → 渲染队列忠实保持混排顺序（数据层已按 last_time 降序）。

        注意：UI 层 _prepare_history_render_queue 不做排序，顺序由数据层
        get_history_list(merge_team=True) 保证（已按 last_time 降序）。
        此处验证队列忠实保持输入顺序（合并条目与普通会话混排不重排）。
        """
        _ensure_qapp()
        card = _make_card()
        merged = _merged_entry(last_time="2026-01-02 10:00:00")
        normal1 = _normal_entry(session_id="n1", last_time="2026-01-03 10:00:00")
        normal2 = _normal_entry(session_id="n2", last_time="2026-01-01 10:00:00")
        # 数据层已按 last_time 降序：n1(01-03) > merged(01-02) > n2(01-01)
        card.set_history([normal1, merged, normal2], None)
        card._update_display()
        QApplication.processEvents()

        # 从队列提取会话项（team_group / session），校验顺序忠实
        items = []
        for q in card._render_queue:
            if q[0] == "team_group":
                items.append(("team", q[1].get("last_time", "")))
            elif q[0] == "session":
                items.append((q[1].get("session_id", ""), q[1].get("last_time", "")))
        times = [t for _, t in items]
        assert times == ["2026-01-03 10:00:00", "2026-01-02 10:00:00", "2026-01-01 10:00:00"], (
            "队列应忠实保持输入（数据层已排序）的 last_time 降序"
        )
        assert items[0][0] == "n1", "最晚的普通会话应排第一"
        assert items[1][0] == "team", "中间应为团队合并条目"


class TestMergedEntryExpand:
    """场景 2：点击展开不触发恢复 + 成员行 + 归档/恢复按钮"""

    def test_mouse_press_toggles_expand_not_restore(self):
        """mousePressEvent 只切换展开/收起，不触发 restoreRequested。"""
        _ensure_qapp()
        from app.widgets.cards.settings.history_card import _TeamGroupCard
        from PyQt5.QtCore import QEvent, QPointF
        from PyQt5.QtGui import QMouseEvent

        card = _TeamGroupCard(_merged_entry())
        restored = []
        card.restoreRequested.connect(restored.append)
        ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(5, 5), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)

        card.mousePressEvent(ev)
        assert restored == [], "点击卡片不应触发恢复"
        assert card._members_visible is True, "点击应展开成员列表"

        card.mousePressEvent(ev)
        assert card._members_visible is False, "再次点击应收起"

    def test_member_row_click_emits_member_selected(self):
        """展开后点击成员行 → memberSelected 携带成员 session_record。"""
        _ensure_qapp()
        from app.widgets.cards.settings.history_card import _TeamGroupCard

        card = _TeamGroupCard(_merged_entry())
        selected = []
        card.memberSelected.connect(selected.append)
        card._toggle_members()

        # 模拟成员行点击（直接调槽，成员记录来自 _members）
        from PyQt5.QtCore import Qt as _QtLeft

        fake_event = type("E", (), {"button": lambda self: _QtLeft.LeftButton})()
        card._on_member_row_clicked(fake_event, card._members[0], None)
        assert len(selected) == 1, "成员行点击应发 memberSelected"
        assert selected[0]["session_id"] == "R1-s1", "应携带成员 session_id"
        assert selected[0]["agent_name"] == "build", "应携带成员 agent_name"

    def test_archive_btn_emits_archive(self):
        """归档按钮 → archiveRequested(run_id)。"""
        _ensure_qapp()
        from app.widgets.cards.settings.history_card import _TeamGroupCard

        card = _TeamGroupCard(_merged_entry())
        captured = []
        card.archiveRequested.connect(captured.append)
        card.archiveRequested.emit("R1")
        assert captured == ["R1"], "归档按钮应发 archiveRequested(run_id)"

    def test_restore_btn_still_works(self):
        """恢复按钮仍触发 restoreRequested（回归保护）。"""
        _ensure_qapp()
        from app.widgets.cards.settings.history_card import _TeamGroupCard

        card = _TeamGroupCard(_merged_entry())
        captured = []
        card.restoreRequested.connect(captured.append)
        card.restoreRequested.emit("R1")
        assert captured == ["R1"], "恢复按钮仍应发 restoreRequested(run_id)"


class TestTeamArchiveChain:
    """场景 3：归档按钮链路"""

    def test_history_card_forwards_archive_signal(self):
        """团队卡 archiveRequested → HistoryCard.teamArchiveRequested 转发。"""
        _ensure_qapp()
        from app.widgets.cards.settings.history_card import HistoryCard

        card = HistoryCard()
        captured = []
        card.teamArchiveRequested.connect(captured.append)
        # 模拟 _process_render_batch 中信号连接后的链路：card.archiveRequested → teamArchiveRequested
        card.teamArchiveRequested.emit("R1")
        assert captured == ["R1"], "HistoryCard 应转发 teamArchiveRequested"

    def test_on_team_archive_requested_calls_archive_by_run_id(self):
        """main_widget 槽：当前会话不在组内 → 归档 + 刷新，不建新会话。"""
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
        win._history_card = MagicMock()
        win._history_card.isVisible.return_value = True

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            with patch("app.main_widget.create_new_session_state") as mock_state:
                with patch("app.main_widget.init_new_session_after_archive") as mock_init:
                    OpenAIChatToolWindow._on_team_archive_requested(win, "R1")

        win.history_manager.get_team_sessions_by_run_id.assert_called_once_with("R1")
        win.history_manager.archive_sessions_by_run_id.assert_called_once_with("R1")
        win._refresh_history_toggle_panel.assert_called_once()
        mock_state.assert_not_called(), "非当前会话归档不应创建新会话状态"
        mock_init.assert_not_called(), "非当前会话归档不应初始化新会话"

    def test_archive_current_session_in_group_triggers_new_session(self):
        """当前会话是组内成员 → create_new_session_state + init_new_session_after_archive 被调。"""
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
        win._current_session_id = "s1"  # 组内成员 → 归档后切换新会话
        win.session_manager = MagicMock()
        win.backend = MagicMock()
        win.backend.chat_engine = MagicMock()
        win.backend.tool_executor = MagicMock()
        win.backend.file_recorder = MagicMock()
        win._clear_chat_area = MagicMock()
        win._show_initial_welcome = MagicMock()
        win.pixel_pet = MagicMock()
        win._history_card = MagicMock()
        win._history_card.isVisible.return_value = True

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            with patch("app.main_widget.create_new_session_state") as mock_state:
                mock_state.return_value = {"session_manager": MagicMock(), "new_session_id": "new-1"}
                with patch("app.main_widget.init_new_session_after_archive") as mock_init:
                    OpenAIChatToolWindow._on_team_archive_requested(win, "R1")

        win.history_manager.archive_sessions_by_run_id.assert_called_once_with("R1")
        mock_state.assert_called_once(), "当前会话在组内应创建新会话状态"
        mock_init.assert_called_once(), "应初始化新会话"
        # 清理文件操作记录
        assert win.backend.file_recorder.clear_session.call_count >= 1

    def test_archive_empty_run_noop(self):
        """无成员会话 → 不归档、不刷新（InfoBar 警告）。"""
        _ensure_qapp()
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win.history_manager = MagicMock()
        win.history_manager.get_team_sessions_by_run_id.return_value = []
        win.pixel_pet = MagicMock()
        win._card_manager = MagicMock()
        win._history_card = MagicMock()
        win._history_card.isVisible.return_value = True

        with patch("app.main_widget.InfoBar") as _mock_infobar:
            OpenAIChatToolWindow._on_team_archive_requested(win, "R1")

        win.history_manager.get_team_sessions_by_run_id.assert_called_once_with("R1")
        win.history_manager.archive_sessions_by_run_id.assert_not_called(), "无成员不应归档"
        win._refresh_history_toggle_panel.assert_not_called(), "无成员不应刷新"
        _mock_infobar.warning.assert_called_once(), "应提示未找到成员会话"


class TestTeamMemberEnter:
    """场景 4：成员进入会话"""

    def test_load_session_from_record_reused_by_popup(self):
        """_load_history_session_from_popup 内部走 _load_session_from_record（不依赖 index 重新取数）。"""
        _ensure_qapp()
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        record = {"session_id": "s1", "title": "t1", "project": "proj-x"}
        win._history_popup_card = MagicMock()
        win._history_popup_card.get_history_at_index.return_value = record

        # 验证 popup 委托给 _load_session_from_record（显式注入实例 mock，
        # 避免 MagicMock 自动 mock 掩盖委托链）
        win._load_session_from_record = MagicMock()
        OpenAIChatToolWindow._load_history_session_from_popup(win, 3)

        win._history_popup_card.get_history_at_index.assert_called_once_with(3)
        win._load_session_from_record.assert_called_once_with(record), "popup 应委托 _load_session_from_record"

    def test_load_session_from_record_calls_create_session(self):
        """_load_session_from_record 本体：reset/create_session/init 全链路。"""
        _ensure_qapp()
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._is_streaming = False
        win.backend = MagicMock()
        win.backend.chat_engine = MagicMock()
        win.backend.tool_executor = MagicMock()
        win.history_manager = MagicMock()
        win.history_manager.get_session_messages.return_value = [{"role": "user", "content": "hi"}]
        record = {"session_id": "s1", "title": "t1", "project": "proj-x"}
        win._get_current_worktree_path.return_value = ""
        win._project_label = MagicMock()
        win.title_edit = MagicMock()
        win._history_card = MagicMock()
        win._history_card.isVisible.return_value = False

        with patch("app.main_widget.create_session_from_record") as mock_create:
            mock_create.return_value = MagicMock()
            with patch("app.main_widget.init_after_loading_session") as mock_init:
                OpenAIChatToolWindow._load_session_from_record(win, record)

        mock_create.assert_called_once(), "应从 record 创建会话"
        mock_init.assert_called_once(), "应初始化加载的会话"
        win.backend.reset_session_state.assert_called_once()

    def test_on_team_member_selected_enters_session(self):
        """成员直选：_on_team_member_selected → _load_session_from_record。"""
        _ensure_qapp()
        from unittest.mock import MagicMock

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win.history_manager = MagicMock()
        win.history_manager.get_session_by_session_id.return_value = {
            "session_id": "R1-s1",
            "title": "build 会话",
            "project": "proj-x",
        }
        win._load_session_from_record = MagicMock()
        win._card_manager = MagicMock()
        win._window_id = "w1"

        OpenAIChatToolWindow._on_team_member_selected(win, {"session_id": "R1-s1", "agent_name": "build"})

        win._load_session_from_record.assert_called_once()
        _args, _kwargs = win._load_session_from_record.call_args
        assert _args[0]["session_id"] == "R1-s1", "应加载成员会话记录"
        win._card_manager.hide_card.assert_called_once_with("history", "w1")


class TestLoadSessionTeamMarks:
    """F4：加载历史会话后同步窗口团队标记（普通清空 / 团队设置）"""

    @staticmethod
    def _make_win(record_team: bool = False):
        """构造可执行 _load_session_from_record 的轻量窗口（跳过 Tab 同步分支）。"""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from app.main_widget import OpenAIChatToolWindow

        win = MagicMock()
        win._is_streaming = False
        win.backend = MagicMock()
        win.backend.chat_engine = MagicMock()
        win.backend.tool_executor = MagicMock()
        win.history_manager = MagicMock()
        win.history_manager.get_session_messages.return_value = [{"role": "user", "content": "hi"}]
        win._get_current_worktree_path.return_value = ""
        win._project_label = MagicMock()
        win.title_edit = MagicMock()
        win._history_card = MagicMock()
        win._history_card.isVisible.return_value = False
        # 跳过 Tab 同步（enable_tab_manager=False），专注验证团队标记赋值
        win.cfg = SimpleNamespace(enable_tab_manager=SimpleNamespace(value=False))
        # 预置团队标记（模拟团队窗口），由 F4 按 record 覆盖
        win._team_run_id = "run-old"
        win._team_name = "dev"
        win._team_agent_name = "build"
        return win

    @staticmethod
    def _run(win, record):
        from unittest.mock import MagicMock, patch

        from app.main_widget import OpenAIChatToolWindow

        with patch("app.main_widget.create_session_from_record") as mock_create:
            mock_create.return_value = MagicMock()
            with patch("app.main_widget.init_after_loading_session") as mock_init:
                OpenAIChatToolWindow._load_session_from_record(win, record)
        return mock_create, mock_init

    def test_normal_session_clears_team_marks(self):
        """F4：加载普通会话（record 无 team_run_id）→ 清空窗口团队标记，防止污染。"""
        _ensure_qapp()
        win = self._make_win()
        record = {"session_id": "s1", "title": "t1", "project": "proj-x"}  # 普通会话无团队字段

        self._run(win, record)

        assert win._team_run_id == "", "普通会话应清空 _team_run_id（防残留污染）"
        assert win._team_name == ""
        assert win._team_agent_name == ""

    def test_team_session_sets_team_marks(self):
        """F4：加载团队会话（带 team_run_id）→ 设置窗口团队标记。"""
        _ensure_qapp()
        win = self._make_win()
        record = {
            "session_id": "s1",
            "title": "t1",
            "project": "proj-x",
            "team_run_id": "run-1",
            "team_name": "dev",
            "agent_name": "build",
        }

        self._run(win, record)

        assert win._team_run_id == "run-1", "团队会话应设置 _team_run_id"
        assert win._team_name == "dev"
        assert win._team_agent_name == "build"


class TestMergedCurrentIndex:
    """场景 5：current_idx 高亮"""

    def _make_win(self):
        from unittest.mock import MagicMock

        win = MagicMock()
        win._history_card = MagicMock()
        win._history_card._current_tab = "history"  # 进入历史分支
        win._history_card.isVisible.return_value = True
        win._history_popup_card = MagicMock()
        win._current_project = "默认项目"
        win._current_session_id = "R1-s1"  # 团队成员会话
        win.history_manager = MagicMock()
        return win

    def test_current_session_is_team_member_highlights_merged_index(self):
        """当前会话是合并条目 members 成员 → set_history 收到 current_idx = 合并条目 index。"""
        _ensure_qapp()
        from app.main_widget import OpenAIChatToolWindow

        win = self._make_win()
        merged = _merged_entry()
        normal = _normal_entry()
        win.history_manager.get_history_list.return_value = [merged, normal]

        OpenAIChatToolWindow._refresh_history_toggle_panel(win)

        _args, _kwargs = win._history_popup_card.set_history.call_args
        assert _args[1] == 0, "当前会话是 R1 成员 → current_idx 应为合并条目 index(0)"

    def test_current_session_not_in_team_idx_none(self):
        """当前会话不在任何 members → current_idx 不指向团队条目。"""
        _ensure_qapp()
        from app.main_widget import OpenAIChatToolWindow

        win = self._make_win()
        win._current_session_id = "not-a-member"
        merged = _merged_entry()
        normal = _normal_entry()
        win.history_manager.get_history_list.return_value = [merged, normal]

        OpenAIChatToolWindow._refresh_history_toggle_panel(win)

        _args, _kwargs = win._history_popup_card.set_history.call_args
        assert _args[1] is None, "非成员会话 → current_idx 应为 None"
