# -*- coding: utf-8 -*-
"""SessionStore pinned 列测试：新库建列 / 老库迁移 / save-load 往返 / update_pinned"""

import pytest


def _fresh_store(tmp_path):
    from app.core.store.session_store import SessionStore

    SessionStore._instance = None
    store = SessionStore(str(tmp_path))
    assert store.is_initialized
    return store


@pytest.fixture
def store(tmp_path):
    s = _fresh_store(tmp_path)
    yield s
    try:
        s.close()
    except Exception:
        pass
    from app.core.store.session_store import SessionStore
    from app.utils.db_manager import DatabaseManager

    SessionStore._instance = None
    DatabaseManager._instance = None


def _mk_session(sid: str, **kw):
    base = {
        "session_id": sid,
        "title": f"会话{sid}",
        "project": "默认项目",
        "messages": [{"role": "user", "content": "hi", "timestamp": "2026-09-12 00:00:00"}],
    }
    base.update(kw)
    return base


def test_new_db_has_pinned_column(store):
    cols = store._db.get_table_info(store.TABLE_NAME)
    col = next(c for c in cols if c["name"] == "pinned")
    assert col["type"] == "INTEGER"


def test_migration_adds_pinned_to_old_db(tmp_path):
    """老库（无 pinned 列）启动时 ALTER 补齐且不丢数据。"""
    import sqlite3

    from app.core.store.session_store import SessionStore
    from app.utils.db_manager import DatabaseManager

    db_path = tmp_path / "sessions.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, title TEXT, project TEXT, "
        "messages BLOB, system_prompt TEXT, compaction_state BLOB, compaction_cache BLOB, "
        "message_count INTEGER, user_edited_title INTEGER, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO sessions (session_id, title, project, messages, message_count, created_at, updated_at) "
        "VALUES ('old1', '老会话', '默认项目', x'00', 1, '2026-09-01 00:00:00', '2026-09-01 00:00:00')"
    )
    conn.commit()
    conn.close()

    SessionStore._instance = None
    DatabaseManager._instance = None
    store = SessionStore(str(tmp_path))
    try:
        cols = [c["name"] for c in store._db.get_table_info(store.TABLE_NAME)]
        assert "pinned" in cols
        got = store.get_session("old1")
        assert got is not None and got.get("title") == "老会话"
        assert got.get("pinned") is False  # 迁移默认值
    finally:
        store.close()
        SessionStore._instance = None
        DatabaseManager._instance = None


def test_pinned_save_load_roundtrip(store):
    store.save_session(_mk_session("s1", pinned=True))
    store.save_session(_mk_session("s2"))
    assert store.get_session("s1").get("pinned") is True
    assert store.get_session("s2").get("pinned") is False


def test_lightweight_list_carries_pinned(store):
    """轻量列表（启动/跨进程重载的加载路径）必须带出 pinned

    轻量投影曾漏 SELECT pinned 列，导致内存历史列表重建后置顶全部丢失；
    且后续 save_session 读到的现值恒为 False，把库中置顶写回 0（不可逆）。
    """
    store.save_session(_mk_session("s1", pinned=True))
    store.save_session(_mk_session("s2"))
    rows = {r["session_id"]: r for r in store.get_sessions_lightweight()}
    assert rows["s1"]["pinned"] is True
    assert rows["s2"]["pinned"] is False


def test_pin_survives_lightweight_reload(store):
    """置顶写入后重载（重启语义）：置顶仍在"""
    store.save_session(_mk_session("s1"))
    assert store.update_session_pinned("s1", True) is True
    rows = {r["session_id"]: r for r in store.get_sessions_lightweight()}
    assert rows["s1"]["pinned"] is True


def test_update_session_pinned(store):
    store.save_session(_mk_session("s1"))
    assert store.update_session_pinned("s1", True) is True
    assert store.get_session("s1").get("pinned") is True
    assert store.update_session_pinned("s1", False) is True
    assert store.get_session("s1").get("pinned") is False
    assert store.update_session_pinned("不存在", True) is False
