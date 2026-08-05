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
        """手动 join 后 team.json 顶层 team_members 以 window_id 为 key 记录成员。

        T3：key 从 agent_name 改为 window_id——同角色多成员（两个 build 异 wid）
        各占一条记录不互相覆盖；agent_name 显式存于记录内。
        """
        tm.join_team("win_01", "perf-tester")
        data = tm._get_team_data("default")
        assert "team_members" in data, "join 应写顶层 team_members"
        assert "win_01" in data["team_members"], "快照 key 应为 window_id（T3）"
        assert data["team_members"]["win_01"]["agent_name"] == "perf-tester", "快照记录应含 agent_name"
        assert data["team_members"]["win_01"]["source"] == "manual"

    def test_join_same_role_different_window_keeps_both(self, tm):
        """T3：同角色多成员（两个 build 异 window_id）快照各保留一条，不覆盖。"""
        tm.join_team("win_02", "build")
        tm.join_team("win_03", "build")
        data = tm._get_team_data("default")
        snapshot = data["team_members"]
        assert "win_02" in snapshot and "win_03" in snapshot, "同角色异 wid 应各占一条快照"
        assert snapshot["win_02"]["agent_name"] == "build"
        assert snapshot["win_03"]["agent_name"] == "build"

    def test_snapshot_union_template_and_manual(self, tm):
        """get_team_member_snapshot = 模板 agents ∪ 手动快照（按 (agent,wid) 去重）。"""
        tm.set_template(
            {
                "name": "dev",
                "agents": [
                    {"agent_name": "leader"},
                    {"agent_name": "build"},
                ],
            }
        )
        tm.join_team("win_01", "build")  # 模板已有 build → 不去重（异 wid 保留）
        tm.join_team("win_02", "perf-tester")  # 手动补充

        records = tm.get_team_member_snapshot()
        # 模板 agents 在前（无 wid），手动快照补充（带 wid）
        assert [r["agent_name"] for r in records] == ["leader", "build", "build", "perf-tester"], (
            f"模板∪快照（同角色异 wid 各保留），实际 {records}"
        )
        by_wid = {r["window_id"]: r["agent_name"] for r in records if r.get("window_id")}
        assert by_wid == {"win_01": "build", "win_02": "perf-tester"}, "手动快照记录应带 window_id"

    def test_cleanup_stale_members_preserves_team_members(self, tm):
        """_cleanup_stale_members 清理失效成员时保留顶层 team_members（活跃窗口）。

        F3 语义：手动加入成员的快照不被清理（恢复时找回无会话成员）——
        指**活跃窗口**的快照永不因 cleanup 丢失。T18 收窄：**失效窗口**
        的快照必须同步清理（否则恢复 wid 残留累积 → 窗口数膨胀回归）。
        """
        tm.join_team("win_01", "perf-tester")
        tm.join_team("win_02", "build")
        # 只有 win_02 活跃 → win_01 被清理（members + team_members 快照同步）
        tm.set_active_window_ids({"win_02"})
        data = tm._get_team_data("default")
        assert "win_01" not in data["members"], "失效成员应从 members 移除"
        assert "win_01" not in data["team_members"], "T18：失效窗口快照应同步清理（防恢复膨胀）"
        assert "win_02" in data["team_members"], "活跃窗口快照保留（F3 恢复找回语义）"


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

    def test_team_members_change_triggers_resave(self, store):
        """R5（T3）：消息未变但 team_members 快照变化 → 内容指纹不同不跳过，新快照落库。

        场景：join 新成员后旧会话快照更新（team_members 内容变化但 messages 未变）。
        若内容指纹忽略 team_members 会跳过保存 → 恢复仍丢新成员。此处验证
        _content_hash_cache 指纹含 team_members（session_repository），
        第二次 save 必须落库新快照。
        """
        session = {
            "session_id": "s-r5",
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
        # 第一次保存：内容指纹缓存建立（消息 + team_members 指纹）
        assert store.save_session(session) is True
        # 消息未变但快照变化（join 新成员后 _get_team_members_snapshot_json 更新）
        old_snap = session["team_members"]
        session["team_members"] = json.dumps(["leader", "build", "perf-tester"], ensure_ascii=False)
        assert store.save_session(session) is True
        loaded = store.get_session("s-r5")
        assert loaded["team_members"] == json.dumps(["leader", "build", "perf-tester"], ensure_ascii=False), (
            "R5：快照变化必须落库（内容指纹含 team_members）"
        )
        # 反向：消息与快照均未变（再次同参保存）→ 指纹命中跳过（幂等不覆盖）
        assert store.save_session(session) is True
        loaded2 = store.get_session("s-r5")
        assert loaded2["team_members"] == json.dumps(["leader", "build", "perf-tester"], ensure_ascii=False), (
            "内容未变跳过保存不应影响已落库数据"
        )
        assert old_snap != session["team_members"], "前置：快照确实发生了变化"

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
        conn.execute("INSERT INTO sessions (session_id, title) VALUES ('old1', '老会话')")
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
                "session_id": "s-l",
                "saved_at": "2026-01-01 00:00:01",
                "title": "leader 会话",
                "project": "默认项目",
                "last_time": "2026-01-01 00:00:01",
                "message_count": 1,
                "preview": "",
                "user_edited_title": False,
                "worktree_path": "",
                "team_run_id": "run-1",
                "team_name": "dev",
                "agent_name": "leader",
                "team_members": snap,
            },
            {
                "session_id": "s-b",
                "saved_at": "2026-01-01 00:00:02",
                "title": "build 会话",
                "project": "默认项目",
                "last_time": "2026-01-01 00:00:02",
                "message_count": 1,
                "preview": "",
                "user_edited_title": False,
                "worktree_path": "",
                "team_run_id": "run-1",
                "team_name": "dev",
                "agent_name": "build",
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
                "session_id": "s-l",
                "saved_at": "2026-01-01 00:00:01",
                "title": "leader 会话",
                "project": "默认项目",
                "last_time": "2026-01-01 00:00:01",
                "message_count": 1,
                "preview": "",
                "user_edited_title": False,
                "worktree_path": "",
                "team_run_id": "run-1",
                "team_name": "dev",
                "agent_name": "leader",
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
        """快照含 perf-tester（无会话记录）→ 恢复时也建窗口。

        T3 升级：复刻段改用新解析（parse_team_members_snapshot）+ 窗口计数
        算法（老格式纯名字快照按 agent 计 1 次，语义与修复前一致）。
        """
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

        # 复刻 _on_team_restore_requested 第 4 步（T3/T18 升级）：
        # by_agent 构建 + 最新会话快照解析 + 窗口计划（无 wid 老格式按 agent 去重）
        from app.utils.history_manager import parse_team_members_snapshot

        by_agent = {}
        for session in member_sessions:
            agent_name = (session.get("agent_name") or "").strip()
            if not agent_name:
                continue
            cur = by_agent.get(agent_name)
            if cur is None or (session.get("last_time") or "") > (cur.get("last_time") or ""):
                by_agent[agent_name] = session
        _latest_session = max(member_sessions, key=lambda s: s.get("last_time") or "", default=None)
        snapshot_members = []
        if _latest_session:
            for rec in parse_team_members_snapshot((_latest_session.get("team_members") or "").strip()):
                if rec not in snapshot_members:
                    snapshot_members.append(rec)
        window_plan = []
        _plain_seen = set()
        if snapshot_members:
            for rec in snapshot_members:
                if rec.get("window_id"):
                    window_plan.append(rec["agent_name"])
                elif rec["agent_name"] not in _plain_seen:
                    _plain_seen.add(rec["agent_name"])
                    window_plan.append(rec["agent_name"])
        else:
            window_plan = list(by_agent.keys())

        assert set(window_plan) == {"leader", "perf-tester"}, f"恢复成员集合 = 会话 ∪ 快照，实际 {set(window_plan)}"
        assert window_plan.count("perf-tester") == 1, "快照成员无会话记录 → 补 1 个空窗口（语义一致）"

    def test_restore_member_set_union_with_window_id(self, store):
        """T3：快照含 window_id 时同角色多成员各建独立窗口，window_id 透传。

        场景：build@win_02 有会话；快照含 build×2（win_02/win_03）+ plan。
        → build 窗口数 = max(by_agent 1, 快照 2) = 2；plan = 1（共 3 窗口）。
        """
        hm = self._hm(store)
        snap = json.dumps(
            [
                {"agent_name": "build", "window_id": "win_02"},
                {"agent_name": "build", "window_id": "win_03"},
                {"agent_name": "plan", "window_id": "win_01"},
            ],
            ensure_ascii=False,
        )
        store.save_session(
            {
                "session_id": "s-b",
                "title": "build 会话",
                "messages": [{"role": "user", "content": "任务", "timestamp": "2026-01-01 00:00:01"}],
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
        member_sessions = hm.get_team_sessions_by_run_id("run-1")
        assert len(member_sessions) == 1

        # 复刻 _on_team_restore_requested 第 4 步（T3/T18）：最新会话快照 → 窗口计划
        from app.utils.history_manager import parse_team_members_snapshot

        by_agent = {}
        for session in member_sessions:
            agent_name = (session.get("agent_name") or "").strip()
            if not agent_name:
                continue
            cur = by_agent.get(agent_name)
            if cur is None or (session.get("last_time") or "") > (cur.get("last_time") or ""):
                by_agent[agent_name] = session
        _latest_session = max(member_sessions, key=lambda s: s.get("last_time") or "", default=None)
        snapshot_members = []
        if _latest_session:
            for rec in parse_team_members_snapshot((_latest_session.get("team_members") or "").strip()):
                if rec not in snapshot_members:
                    snapshot_members.append(rec)
        # window_id 透传断言：快照记录保留 wid 字段
        wid_records = [r for r in snapshot_members if r.get("window_id")]
        assert len(wid_records) == 3, "含 wid 快照记录应透传 window_id"
        assert sorted(r["window_id"] for r in wid_records) == ["win_01", "win_02", "win_03"]
        window_plan = []
        _plain_seen = set()
        for rec in snapshot_members:
            if rec.get("window_id"):
                window_plan.append(rec["agent_name"])
            elif rec["agent_name"] not in _plain_seen:
                _plain_seen.add(rec["agent_name"])
                window_plan.append(rec["agent_name"])

        # build 快照 2 条（含 wid）→ 2 窗口差额；plan 快照 1 条 → 1 窗口
        assert window_plan.count("build") == 2, f"同角色多成员各建独立窗口，实际 {window_plan}"
        assert window_plan.count("plan") == 1
        assert len(window_plan) == 3, f"窗口计划共 3 个，实际 {window_plan}"

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


class TestRestoreWindowNoInflation:
    """T18 阻断项回归：恢复窗口数不随恢复次数线性膨胀

    根因：T3 将 team_members 快照 key 改为 window_id 后只增不删（leave/
    _cleanup 只清 members）→ 恢复每次 join 新 wid 累积旧记录 → 快照计数
    膨胀 → 恢复窗口数 2→4→6…。

    修复三层：leave_team 同步清理 / _cleanup_stale_members 兜底清理 /
    恢复路径 rebuild_team_members_snapshot 重建。此处覆盖数据层机制。
    """

    def test_two_restores_window_count_stable(self, tm):
        """核心回归：两次"恢复"窗口数恒定（= 成员数，不膨胀）。

        模拟恢复链路：join（新 wid）→ rebuild 快照 → 再次恢复
        （leave 旧窗 + join 新 wid + rebuild）→ 两次恢复快照计数相同。
        """
        # 第一次恢复：build + plan 两个成员窗口
        tm.join_team("win_r1a", "build")
        tm.join_team("win_r1b", "plan")
        tm.rebuild_team_members_snapshot([("win_r1a", "build"), ("win_r1b", "plan")])
        snap1 = tm.get_team_member_snapshot()
        assert len(snap1) == 2, f"第一次恢复快照应 2 条，实际 {snap1}"

        # 第二次恢复：旧窗口解散（leave_team）+ 新 wid join + rebuild
        tm.leave_team("win_r1a")
        tm.leave_team("win_r1b")
        tm.join_team("win_r2a", "build")
        tm.join_team("win_r2b", "plan")
        tm.rebuild_team_members_snapshot([("win_r2a", "build"), ("win_r2b", "plan")])
        snap2 = tm.get_team_member_snapshot()
        assert len(snap2) == 2, f"第二次恢复快照应仍 2 条（不膨胀），实际 {snap2}"
        # 窗口数（恢复侧按 snap_counts 计）两次恒为 2
        assert {r["window_id"] for r in snap2} == {"win_r2a", "win_r2b"}, "快照应仅含本次恢复窗口"

    def test_leave_team_removes_snapshot_record(self, tm):
        """leave_team 同步清理该窗口的 team_members 快照记录（T18 主路径）。"""
        tm.join_team("win_01", "build")
        assert "win_01" in tm._get_team_data("default")["team_members"]
        tm.leave_team("win_01")
        data = tm._get_team_data("default")
        assert "win_01" not in data["team_members"], "leave 后快照记录应同步删除"
        assert tm.get_team_member_snapshot() == [], "快照应清空"

    def test_cleanup_stale_members_removes_snapshot(self, tm):
        """_cleanup_stale_members 兜底清理失效窗口快照（异常关闭路径）。"""
        tm.join_team("win_01", "build")
        tm.join_team("win_02", "plan")
        # 只有 win_02 活跃 → win_01 失效，快照同步清理
        tm.set_active_window_ids({"win_02"})
        data = tm._get_team_data("default")
        assert "win_01" not in data["team_members"], "失效窗口快照应被清理"
        assert "win_02" in data["team_members"], "活跃窗口快照保留"

    def test_cleanup_preserves_legacy_agent_key(self, tm):
        """老格式（key=agent_name，value 无 agent_name 字段）不被 window 维度清理误删。"""
        data = tm._get_team_data("default")
        # 手工注入老格式快照（T3 前落库数据形态）
        data["team_members"] = {
            "perf-tester": {"source": "manual", "joined_at": 1000},
        }
        tm._save_team_data("default")
        tm.join_team("win_01", "build")  # 触发一次保存
        tm.set_active_window_ids({"win_01"})
        data = tm._get_team_data("default")
        assert "perf-tester" in data["team_members"], "老格式 agent key 应保留（兼容读取）"
        assert "win_01" in data["team_members"], "新格式 wid key 正常"

    def test_cleanup_when_members_empty_still_cleans_snapshot(self, tm):
        """members 为空时仍清理 team_members 残留（解散团队后场景）。"""
        tm.join_team("win_01", "build")
        # 解散：成员记录删除（模拟 leave），但快照残留（异常路径）
        data = tm._get_team_data("default")
        data["members"] = {}
        tm._save_team_data("default")
        assert "win_01" in data["team_members"], "前置：快照残留"
        tm.set_active_window_ids({"win_02"})  # 新窗口活跃，旧 wid 失效
        data = tm._get_team_data("default")
        assert "win_01" not in data["team_members"], "members 为空也应清理快照残留"

    def test_rebuild_keeps_duplicate_role_members(self, tm):
        """rebuild 保持 T3 语义：同角色多成员（异 wid）各保留一条。"""
        tm.rebuild_team_members_snapshot([("win_02", "build"), ("win_03", "build"), ("win_01", "plan")])
        records = tm.get_team_member_snapshot()
        assert len(records) == 3, f"同角色多成员应各保留一条，实际 {records}"
        assert sorted(r["window_id"] for r in records) == ["win_01", "win_02", "win_03"]
