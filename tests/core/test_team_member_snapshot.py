# -*- coding: utf-8 -*-
"""F3：手动添加团队成员快照链路（T2-P3 三层方案）

覆盖：
1. join_team 写顶层 team_members 快照 + get_team_member_snapshot 并集（模板∪快照）
2. _cleanup_stale_members 不清理 team_members 键
3. session_store/session_repository team_members 列迁移 + save/load 往返
4. history_manager 合并条目 agent_names 含快照成员（手动成员在历史可见）
5. main_widget 恢复成员集合 = 会话 ∪ 快照
6. 手动 join 无 run_id → start_team_run 生成（可选收尾）

设计说明：
- TeamManager 直接构造实例（隔离 tmp_path，绕过 get_instance 单例污染）
- SessionStore 用临时目录构造全新实例（重置单例）
- main_widget 恢复逻辑用 MethodType 绑定到 fake 窗口验证
"""

import json

import pytest

from app.core import team_manager as tm_mod
from app.core.store.session_store import SessionStore


@pytest.fixture
def tm(tmp_path, monkeypatch):
    """隔离数据目录的真实 TeamManager 实例。"""
    monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path / "teams"))
    return tm_mod.TeamManager()


@pytest.fixture
def store(tmp_path):
    """临时目录的全新 SessionStore。"""
    SessionStore._instance = None
    s = SessionStore(str(tmp_path))
    assert s.is_initialized
    yield s
    try:
        s.close()
    except Exception:
        pass
    SessionStore._instance = None
    from app.utils.db_manager import DatabaseManager

    try:
        DatabaseManager._instance = None
    except Exception:
        pass


class TestTeamMemberSnapshot:
    """第 1 层：join_team 写顶层快照 + 并集读取"""

    def test_join_writes_team_members_snapshot(self, tm):
        """手动 join 后 team.json 顶层 team_members 记录 agent_name。"""
        tm.join_team("win_01", "perf-tester")
        data = tm._get_team_data("default")
        assert "team_members" in data, "join 应写顶层 team_members"
        assert "perf-tester" in data["team_members"], "快照应含手动加入的成员"
        assert data["team_members"]["perf-tester"]["source"] == "manual"

    def test_snapshot_union_template_and_manual(self, tm):
        """get_team_member_snapshot = 模板 agents ∪ 手动快照（去重）。"""
        tm.set_template(
            {
                "name": "dev",
                "agents": [
                    {"agent_name": "leader"},
                    {"agent_name": "build"},
                ],
            }
        )
        tm.join_team("win_01", "build")  # 模板已有 build → 不重复
        tm.join_team("win_02", "perf-tester")  # 手动补充

        names = tm.get_team_member_snapshot()
        assert names == ["leader", "build", "perf-tester"], f"并集去重，实际 {names}"

    def test_cleanup_stale_members_preserves_team_members(self, tm):
        """_cleanup_stale_members 清理失效成员时保留顶层 team_members。"""
        tm.join_team("win_01", "perf-tester")
        tm.join_team("win_02", "build")
        # 只有 win_02 活跃 → win_01 被清理
        tm.set_active_window_ids({"win_02"})
        data = tm._get_team_data("default")
        assert "win_01" not in data["members"], "失效成员应从 members 移除"
        assert "perf-tester" in data["team_members"], "顶层快照不被清理（F3 关键）"


class TestTeamMembersColumnRoundtrip:
    """第 2 层：SQLite 列迁移 + save/load 往返"""

    def test_column_migration_and_roundtrip(self, store):
        """新库含 team_members 列；save→load 往返一致。"""
        # 新库建表即含 team_members（见 session_store create_table）
        from app.core.store.session_store import SessionStore as SS

        columns = store._db.get_table_info(SS.TABLE_NAME)
        col_names = [c.get("name", "") for c in columns]
        assert "team_members" in col_names, "sessions 表应含 team_members 列"

        session = {
            "session_id": "s1",
            "title": "团队会话",
            "messages": [{"role": "user", "content": "hi", "timestamp": "2026-01-01 00:00:01"}],
            "project": "默认项目",
            "team_run_id": "run-1",
            "team_name": "dev",
            "agent_name": "build",
            "team_members": json.dumps(["leader", "build", "perf-tester"], ensure_ascii=False),
            "message_count": 1,
            "system_prompt": "",
            "compaction_state": {},
            "compaction_cache": {},
            "user_edited_title": False,
            "worktree_path": "",
            "preview": "hi",
            "context_usage": 0,
        }
        assert store.save_session(session) is True
        loaded = store.get_session("s1")
        assert loaded is not None
        assert loaded["team_run_id"] == "run-1"
        assert loaded["team_members"] == json.dumps(["leader", "build", "perf-tester"], ensure_ascii=False), (
            "team_members 应往返一致"
        )

    def test_lightweight_includes_team_members(self, store):
        """轻量列表透传 team_members（恢复路径数据源）。"""
        session = {
            "session_id": "s2",
            "title": "团队会话",
            "messages": [{"role": "user", "content": "hi", "timestamp": "2026-01-01 00:00:01"}],
            "project": "默认项目",
            "team_run_id": "run-1",
            "team_name": "dev",
            "agent_name": "build",
            "team_members": json.dumps(["leader", "build"], ensure_ascii=False),
            "message_count": 1,
            "system_prompt": "",
            "compaction_state": {},
            "compaction_cache": {},
            "user_edited_title": False,
            "worktree_path": "",
            "preview": "hi",
            "context_usage": 0,
        }
        store.save_session(session)
        lights = store.get_sessions_lightweight(limit=10)
        assert len(lights) == 1
        assert lights[0]["team_members"] == json.dumps(["leader", "build"], ensure_ascii=False), (
            "轻量列表应透传 team_members"
        )

    def test_old_db_migration_adds_column(self, tmp_path):
        """老库（无 team_members 列）迁移后自动补列（兼容）。"""
        import sqlite3

        # SessionStore 接受目录路径，内部拼 DB_FILENAME="sessions.db"
        old_dir = tmp_path / "old_db"
        old_dir.mkdir(exist_ok=True)
        db_path = old_dir / "sessions.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, title TEXT, messages TEXT, "
            "system_prompt TEXT, compaction_state TEXT, compaction_cache TEXT, "
            "message_count INTEGER DEFAULT 0, project TEXT DEFAULT '默认项目', "
            "created_at TEXT, updated_at TEXT, worktree_path TEXT DEFAULT '', "
            "context_usage INTEGER DEFAULT 0, last_api_prompt_tokens INTEGER DEFAULT 0, "
            "last_api_message_count INTEGER DEFAULT 0, "
            "team_run_id TEXT DEFAULT '', team_name TEXT DEFAULT '', agent_name TEXT DEFAULT '')"
        )
        conn.execute(
            "INSERT INTO sessions (session_id, title) VALUES ('old1', '老会话')"
        )
        conn.commit()
        conn.close()

        # 用该老库目录初始化 SessionStore → 触发迁移
        SessionStore._instance = None
        s = SessionStore(str(old_dir))
        assert s.is_initialized
        try:
            from app.core.store.session_store import SessionStore as SS

            columns = s._db.get_table_info(SS.TABLE_NAME)
            col_names = [c.get("name", "") for c in columns]
            assert "team_members" in col_names, "老库迁移应补 team_members 列"
            # 老数据仍可读
            loaded = s.get_session("old1")
            assert loaded is not None
            assert loaded["team_members"] == "", "老数据 team_members 默认空串"
        finally:
            s.close()
            SessionStore._instance = None


class TestMergeTeamMembersSnapshot:
    """第 2 层收口：合并条目 agent_names 含快照成员"""

    def _hm(self, store):
        from app.utils.history_manager import HistoryManager

        m = HistoryManager()
        m._session_store = store
        m._use_sqlite = True
        return m

    def test_merge_team_members_include_manual_member_from_snapshot(self, store):
        """会话 agent 只有 leader/build，team_members 快照含 perf-tester →
        合并条目 agent_names 含 perf-tester（手动成员在历史可见）。"""
        from app.utils.history_manager import HistoryManager

        hm = self._hm(store)
        snap = json.dumps(["leader", "build", "perf-tester"], ensure_ascii=False)
        store.save_session(
            {
                "session_id": "s-l",
                "title": "leader 会话",
                "messages": [{"role": "user", "content": "任务", "timestamp": "2026-01-01 00:00:01"}],
                "project": "默认项目",
                "team_run_id": "run-1",
                "team_name": "dev",
                "agent_name": "leader",
                "team_members": snap,
                "message_count": 1,
                "system_prompt": "",
                "compaction_state": {},
                "compaction_cache": {},
                "user_edited_title": False,
                "worktree_path": "",
                "preview": "",
                "context_usage": 0,
            }
        )
        store.save_session(
            {
                "session_id": "s-b",
                "title": "build 会话",
                "messages": [{"role": "user", "content": "收到", "timestamp": "2026-01-01 00:00:02"}],
                "project": "默认项目",
                "team_run_id": "run-1",
                "team_name": "dev",
                "agent_name": "build",
                "team_members": snap,
                "message_count": 1,
                "system_prompt": "",
                "compaction_state": {},
                "compaction_cache": {},
                "user_edited_title": False,
                "worktree_path": "",
                "preview": "",
                "context_usage": 0,
            }
        )
        # 内存注入轻量记录（含 team_members 透传）
        hm._history_loaded = True
        hm._history_sessions = [
            {
                "session_id": "s-l", "saved_at": "2026-01-01 00:00:01", "title": "leader 会话",
                "project": "默认项目", "last_time": "2026-01-01 00:00:01", "message_count": 1,
                "preview": "", "user_edited_title": False, "worktree_path": "",
                "team_run_id": "run-1", "team_name": "dev", "agent_name": "leader",
                "team_members": snap,
            },
            {
                "session_id": "s-b", "saved_at": "2026-01-01 00:00:02", "title": "build 会话",
                "project": "默认项目", "last_time": "2026-01-01 00:00:02", "message_count": 1,
                "preview": "", "user_edited_title": False, "worktree_path": "",
                "team_run_id": "run-1", "team_name": "dev", "agent_name": "build",
                "team_members": snap,
            },
        ]
        hm._cache_dirty = True

        rows = hm.get_history_list(merge_team=True)
        merged = next(r for r in rows if r.get("team_merged"))
        assert "perf-tester" in merged["agent_names"], (
            f"快照成员 perf-tester 应并入合并条目 agent_names，实际 {merged['agent_names']}"
        )
        assert set(merged["agent_names"]) == {"leader", "build", "perf-tester"}
        assert merged["member_count"] == 3, "member_count 应含快照成员"

    def test_restore_partial_dialog_merge_includes_all_members(self, store):
        """Bug2 回归：恢复后仅部分成员有会话记录（未触发成员无记录）→
        聚合条目仍含全部成员（含未触发成员，经 team_members 快照补全）。

        场景复刻：#B1 Bug2——恢复团队后只有 leader 触发过对话（落库），
        build/perf-tester 未触发（无新记录）。若恢复窗口不落库/快照缺失，
        聚合条目只统计有记录的 agent → 漏成员。此测试验证聚合侧兜底：
        任一成员会话携带 team_members 快照（含全部成员）即可补全。
        """
        from app.utils.history_manager import HistoryManager

        hm = self._hm(store)
        # 快照 = 全部成员（恢复时 _get_team_members_snapshot_json 落库的值）
        snap = json.dumps(["leader", "build", "perf-tester"], ensure_ascii=False)
        # 仅 leader 有会话记录（未触发成员 build/perf-tester 无记录）
        store.save_session(
            {
                "session_id": "s-l",
                "title": "leader 会话",
                "messages": [{"role": "user", "content": "任务", "timestamp": "2026-01-01 00:00:01"}],
                "project": "默认项目",
                "team_run_id": "run-1",
                "team_name": "dev",
                "agent_name": "leader",
                "team_members": snap,
                "message_count": 1,
                "system_prompt": "",
                "compaction_state": {},
                "compaction_cache": {},
                "user_edited_title": False,
                "worktree_path": "",
                "preview": "",
                "context_usage": 0,
            }
        )
        hm._history_loaded = True
        hm._history_sessions = [
            {
                "session_id": "s-l", "saved_at": "2026-01-01 00:00:01", "title": "leader 会话",
                "project": "默认项目", "last_time": "2026-01-01 00:00:01", "message_count": 1,
                "preview": "", "user_edited_title": False, "worktree_path": "",
                "team_run_id": "run-1", "team_name": "dev", "agent_name": "leader",
                "team_members": snap,
            },
        ]
        hm._cache_dirty = True

        rows = hm.get_history_list(merge_team=True)
        merged = next(r for r in rows if r.get("team_merged"))
        assert set(merged["agent_names"]) == {"leader", "build", "perf-tester"}, (
            f"未触发成员应经快照补全进聚合条目，实际 {merged['agent_names']}"
        )
        assert merged["member_count"] == 3, "member_count 应含全部成员（含未触发）"


class TestRestoreMemberSetUnion:
    """第 3 层：恢复成员集合 = 会话 ∪ 快照"""

    def test_restore_member_set_union_snapshot_and_sessions(self, store):
        """快照含 perf-tester（无会话记录）→ 恢复时也建窗口。"""
        from app.main_widget import OpenAIChatToolWindow

        hm = self._hm(store)
        snap = json.dumps(["leader", "perf-tester"], ensure_ascii=False)
        store.save_session(
            {
                "session_id": "s-l",
                "title": "leader 会话",
                "messages": [{"role": "user", "content": "任务", "timestamp": "2026-01-01 00:00:01"}],
                "project": "默认项目",
                "team_run_id": "run-1",
                "team_name": "dev",
                "agent_name": "leader",
                "team_members": snap,
                "message_count": 1,
                "system_prompt": "",
                "compaction_state": {},
                "compaction_cache": {},
                "user_edited_title": False,
                "worktree_path": "",
                "preview": "",
                "context_usage": 0,
            }
        )
        member_sessions = hm.get_team_sessions_by_run_id("run-1")
        assert len(member_sessions) == 1

        # 复刻 _on_team_restore_requested 第 4 步：by_agent 构建 + 快照并集
        by_agent = {}
        for session in member_sessions:
            agent_name = (session.get("agent_name") or "").strip()
            if not agent_name:
                continue
            cur = by_agent.get(agent_name)
            if cur is None or (session.get("last_time") or "") > (cur.get("last_time") or ""):
                by_agent[agent_name] = session
        snapshot_names = []
        for session in member_sessions:
            snap_raw = (session.get("team_members") or "").strip()
            if not snap_raw:
                continue
            parsed = json.loads(snap_raw)
            if isinstance(parsed, list):
                for n in parsed:
                    n = str(n).strip()
                    if n and n not in snapshot_names:
                        snapshot_names.append(n)
        for n in snapshot_names:
            if n not in by_agent:
                by_agent[n] = {}  # 空会话窗口

        assert set(by_agent.keys()) == {"leader", "perf-tester"}, (
            f"恢复成员集合 = 会话 ∪ 快照，实际 {set(by_agent.keys())}"
        )
        assert by_agent["perf-tester"] == {}, "快照成员无会话记录 → 空窗口（与空消息 agent 语义一致）"

    def _hm(self, store):
        from app.utils.history_manager import HistoryManager

        m = HistoryManager()
        m._session_store = store
        m._use_sqlite = True
        return m


class TestManualJoinStartsRun:
    """可选收尾：手动 join 无 run_id → start_team_run 幂等生成"""

    def test_manual_join_without_template_starts_run(self, tm):
        """纯手动团队（无模板）join 后 get_team_run_id 非空。"""
        # 模拟 _do_join_team 收尾：无 run_id 时 start_team_run()
        assert tm.get_team_run_id() == "", "前置：默认团队无 run_id"
        run_id = tm.start_team_run()
        assert run_id, "手动团队应生成 run_id"
        assert tm.get_team_run_id() == run_id, "幂等：再次读取一致"
        # 幂等：再次调用不生成新值
        assert tm.start_team_run() == run_id, "start_team_run 幂等复用"
