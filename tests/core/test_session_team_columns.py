# -*- coding: utf-8 -*-
"""SessionStore 团队元数据列（team_run_id / team_name / agent_name）测试

方案 A 团队会话恢复（阶段 1 数据层）：
- 新库建表应含 3 个团队列（TEXT DEFAULT ''）
- 老库（无 3 列）ALTER 迁移后兼容（非破坏性）
- save / load 往返：写入 team_run_id / team_name / agent_name 后读取一致
- 轻量列表（get_all_lightweight）同样透传 3 列
"""

import pytest

TEAM_COLS = ("team_run_id", "team_name", "agent_name")


def _fresh_store(tmp_path):
    """用临时目录构造全新 SessionStore（重置单例，避免污染真实数据库）。"""
    from app.core.store.session_store import SessionStore

    SessionStore._instance = None
    store = SessionStore(str(tmp_path))
    assert store.is_initialized, "SessionStore 应初始化成功"
    return store


@pytest.fixture
def store(tmp_path):
    s = _fresh_store(tmp_path)
    yield s
    try:
        s.close()
    except Exception:
        pass
    # 清理单例，避免影响其他测试
    from app.core.store.session_store import SessionStore

    SessionStore._instance = None
    from app.utils.db_manager import DatabaseManager

    try:
        DatabaseManager._instance = None
    except Exception:
        pass


def test_new_db_has_team_columns(store):
    """新库建表应包含 3 个团队列（TEXT DEFAULT ''）。"""
    cols = store._db.get_table_info(store.TABLE_NAME)
    col_names = [c["name"] for c in cols]
    for col in TEAM_COLS:
        assert col in col_names, f"新库 sessions 表缺少列 {col}"
        info = next(c for c in cols if c["name"] == col)
        assert info["type"] == "TEXT"
        # SQLite PRAGMA table_info 的 default_value 对字符串默认值返回带引号形式 "''"
        assert info["default_value"] in ("", "''"), f"列 {col} 默认值应为空串"


def test_migration_adds_team_columns_to_old_db(tmp_path):
    """老库（无团队列）启动时应自动 ALTER 补齐 3 列，且不丢已有数据。"""
    import sqlite3

    from app.core.store.session_store import SessionStore

    # 模拟老库：手动建不含团队列的 sessions 表 + 插入一条历史数据
    db_file = tmp_path / "sessions.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            title TEXT,
            messages TEXT,
            system_prompt TEXT,
            compaction_state TEXT,
            compaction_cache TEXT,
            message_count INTEGER DEFAULT 0,
            project TEXT DEFAULT '默认项目',
            created_at TEXT,
            updated_at TEXT,
            worktree_path TEXT DEFAULT '',
            context_usage INTEGER DEFAULT 0,
            last_api_prompt_tokens INTEGER DEFAULT 0,
            last_api_message_count INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        "INSERT INTO sessions (session_id, title, project, created_at, updated_at) "
        "VALUES ('old-1', '历史会话', '默认项目', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()

    SessionStore._instance = None
    store = SessionStore(str(tmp_path))
    try:
        assert store.is_initialized
        cols = store._db.get_table_info(store.TABLE_NAME)
        col_names = [c["name"] for c in cols]
        for col in TEAM_COLS:
            assert col in col_names, f"老库迁移后缺少列 {col}"

        # 老数据保留且团队列默认空串
        session = store.get_session("old-1")
        assert session is not None
        assert session["title"] == "历史会话"
        assert session["team_run_id"] == ""
        assert session["team_name"] == ""
        assert session["agent_name"] == ""
    finally:
        store.close()
        SessionStore._instance = None
        from app.utils.db_manager import DatabaseManager

        DatabaseManager._instance = None


def _sample_session(team=False):
    session = {
        "session_id": "t1" if team else "n1",
        "title": "团队会话" if team else "普通会话",
        "project": "默认项目",
        "messages": [{"role": "user", "content": "hello", "timestamp": "2026-01-01 00:00:00"}],
        "system_prompt": "",
        "compaction_state": {},
        "compaction_cache": {},
        "message_count": 1,
        "user_edited_title": False,
        "worktree_path": "",
        "preview": "hello",
        "context_usage": 0,
        "last_api_prompt_tokens": 0,
        "last_api_message_count": 0,
    }
    if team:
        session.update(
            {
                "team_run_id": "run-2026-01-01-abc",
                "team_name": "dev-team",
                "agent_name": "build",
            }
        )
    return session


def test_save_load_roundtrip_team_metadata(store):
    """写入 team_run_id/team_name/agent_name 后 get 读取一致。"""
    session = _sample_session(team=True)
    assert store.save_session(session) is True

    loaded = store.get_session("t1")
    assert loaded is not None
    assert loaded["team_run_id"] == "run-2026-01-01-abc"
    assert loaded["team_name"] == "dev-team"
    assert loaded["agent_name"] == "build"
    # 其他字段不受影响
    assert loaded["title"] == "团队会话"
    assert loaded["messages"][0]["content"] == "hello"


def test_save_load_roundtrip_non_team_empty(store):
    """非团队会话团队列保持空串（老行为兼容）。"""
    session = _sample_session(team=False)
    assert store.save_session(session) is True

    loaded = store.get_session("n1")
    assert loaded is not None
    assert loaded["team_run_id"] == ""
    assert loaded["team_name"] == ""
    assert loaded["agent_name"] == ""


def test_get_all_lightweight_passes_team_columns(store):
    """轻量列表（get_all_lightweight）同样透传 3 个团队列。"""
    session = _sample_session(team=True)
    assert store.save_session(session) is True

    rows = store.session_repo.get_all_lightweight(limit=10)
    assert rows, "应有至少 1 条轻量记录"
    row = next(r for r in rows if r["session_id"] == "t1")
    assert row["team_run_id"] == "run-2026-01-01-abc"
    assert row["team_name"] == "dev-team"
    assert row["agent_name"] == "build"


def test_history_manager_save_session_passes_team_fields(store):
    """HistoryManager.save_session 的 team 参数透传到 session_record。"""
    from app.utils.history_manager import HistoryManager

    hm = HistoryManager()
    try:
        hm.save_session(
            [{"role": "user", "content": "hi", "timestamp": "2026-01-01 00:00:00"}],
            title="团队会话",
            session_id="hm1",
            team_run_id="run-x",
            team_name="dev-team",
            agent_name="plan",
        )
        session = hm.get_session_by_session_id("hm1")
        assert session is not None
        assert session["team_run_id"] == "run-x"
        assert session["team_name"] == "dev-team"
        assert session["agent_name"] == "plan"

        # 轻量列表透传
        rows = hm.get_history_list()
        row = next(r for r in rows if r["session_id"] == "hm1")
        assert row["team_run_id"] == "run-x"
        assert row["team_name"] == "dev-team"
        assert row["agent_name"] == "plan"
    finally:
        hm.flush()
