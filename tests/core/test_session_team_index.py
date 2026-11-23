# -*- coding: utf-8 -*-
"""sessions 表 team_run_id 索引回归测试

背景（2026-11-17）：打开历史卡片时主线程阻塞 544ms。根因是
get_team_first_question_candidates 按 team_run_id 过滤却无对应索引，
SQLite 只能 SCAN（实测 527MB / 2607 行库单次 15.7ms，122 个团队 run
的 N+1 合计 1779ms）。补 (team_run_id, updated_at DESC) 复合索引后
同一循环降到 4.3ms。

本测试锁定：
- 新库初始化后存在 idx_team_run
- 团队首问候选查询走 SEARCH 索引而非 SCAN
"""

import pytest

QUERY = (
    "SELECT first_user_ts, first_user_msg FROM sessions "
    "WHERE team_run_id = ? AND first_user_msg IS NOT NULL AND first_user_msg != '' "
    "ORDER BY updated_at DESC"
)


@pytest.fixture
def store(tmp_path):
    from app.core.store.session_store import SessionStore

    SessionStore._instance = None
    s = SessionStore(str(tmp_path))
    assert s.is_initialized, "SessionStore 应初始化成功"
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


def _cell(row, name: str, idx: int):
    """兼容 sqlite3.Row 与 tuple 两种返回形态取列。"""
    try:
        return row[name]
    except KeyError, IndexError, TypeError:
        return row[idx]


def test_team_run_index_created(store):
    """新库应创建 idx_team_run 索引，覆盖 team_run_id 过滤 + updated_at 排序。"""
    success, rows = store._db.execute_sql("PRAGMA index_list('sessions')")
    assert success
    names = {_cell(row, "name", 1) for row in rows}
    assert "idx_team_run" in names, f"缺少 idx_team_run 索引，现有索引: {sorted(names)}"


def test_team_first_question_query_uses_index(store):
    """团队首问候选查询必须走索引 SEARCH，不得全表 SCAN。"""
    import sqlite3

    # execute_sql 把 EXPLAIN 当写语句（返回 rowcount），故用独立连接取查询计划
    conn = sqlite3.connect(store._db_path)
    try:
        rows = conn.execute("EXPLAIN QUERY PLAN " + QUERY, ("run-x",)).fetchall()
    finally:
        conn.close()
    plan = " | ".join(str(row[-1]) for row in rows)
    assert "SCAN sessions" not in plan, f"团队首问查询仍全表扫描: {plan}"
    assert "idx_team_run" in plan, f"团队首问查询未命中 idx_team_run: {plan}"
