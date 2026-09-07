# -*- coding: utf-8 -*-
"""message_extras：UI 态字段剥离 / 读回 / 兼容 测试

覆盖 specs/2026-09-07-message-extras-offload-design.md §7：
- 新库自动建 session_msg_extras 表
- save 剥离保活窗外三字段 + 主 blob 轻量化 + _x_idx 哨兵
- 保活窗内字段保留、内存事实源不被污染
- load_msg_extras 读回 / get_full_messages 合并
- 老数据（主 blob 带字段）读时兼容，首次保存收敛
- delete 级联清理 extras、compaction 重写后 extras 对齐
"""

import pytest


def _fresh_store(tmp_path):
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
    from app.core.store.session_store import SessionStore
    from app.utils.db_manager import DatabaseManager

    SessionStore._instance = None
    DatabaseManager._instance = None


def _msg(role, content, **extra):
    m = {"role": role, "content": content, "timestamp": "2026-01-01 00:00:00"}
    m.update(extra)
    return m


def _session(session_id, messages):
    return {
        "session_id": session_id,
        "title": "t",
        "project": "默认项目",
        "messages": messages,
        "system_prompt": "",
        "compaction_state": {},
        "compaction_cache": {},
        "message_count": len(messages),
        "user_edited_title": False,
        "worktree_path": "",
        "preview": "",
        "context_usage": 0,
        "last_api_prompt_tokens": 0,
        "last_api_message_count": len(messages),
    }


def test_extras_table_created(store):
    ok, rows = store._db.execute_sql("SELECT name FROM sqlite_master WHERE type='table' AND name='session_msg_extras'")
    assert ok and rows, "session_msg_extras 表应自动创建"


def test_extras_index_created(store):
    ok, rows = store._db.execute_sql(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_msg_extras_session'"
    )
    assert ok and rows, "idx_msg_extras_session 索引应自动创建"


def test_extras_table_schema(store):
    """表 schema：复合主键 (session_id, msg_idx, field) + value BLOB"""
    cols = store._db.get_table_info("session_msg_extras")
    col_names = {c["name"] for c in cols}
    assert col_names == {"session_id", "msg_idx", "field", "value"}, f"列名集合不符：{col_names}"
    # 主键集合检查
    pk_cols = [c["name"] for c in cols if c["pk"] > 0]
    assert pk_cols == ["session_id", "msg_idx", "field"], f"复合主键顺序不符：{pk_cols}"
    # value 类型应为 BLOB
    value_col = next(c for c in cols if c["name"] == "value")
    assert value_col["type"].upper() == "BLOB", f"value 应为 BLOB，实际 {value_col['type']}"
