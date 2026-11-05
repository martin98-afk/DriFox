# -*- coding: utf-8 -*-
"""storage.db 受限代理：语句白名单 + 表名前缀约束

覆盖：表名前缀、系统表、库级语句（ATTACH/PRAGMA/VACUUM）、多语句注入、
只读便捷入口的错误吞并；执行路径用 monkeypatch 替换 DatabaseManager 验证透传。
"""

import pytest

from app.plugins.capabilities.providers.db_guard import PluginDBProxy, normalize_plugin_name


@pytest.fixture()
def db():
    return PluginDBProxy("system-subagent")


def test_table_prefix_normalized(db):
    assert db.table_prefix == "plugin_system_subagent_"
    assert db.table("task") == "plugin_system_subagent_task"
    assert db.table("_task_") == "plugin_system_subagent_task"
    assert normalize_plugin_name("My.Plugin Name") == "my_plugin_name"
    assert normalize_plugin_name("") == "plugin"


def test_select_and_dml_on_own_table_pass(db):
    assert db.check_sql("SELECT * FROM plugin_system_subagent_task") == ""
    assert db.check_sql("INSERT INTO plugin_system_subagent_task (id) VALUES (?)") == ""
    assert db.check_sql("UPDATE plugin_system_subagent_task SET id=1 WHERE id=2") == ""
    assert db.check_sql("DELETE FROM plugin_system_subagent_task") == ""
    assert db.check_sql("CREATE TABLE IF NOT EXISTS plugin_system_subagent_task (id TEXT)") == ""


def test_index_on_own_table_pass(db):
    assert db.check_sql("CREATE INDEX idx_task ON plugin_system_subagent_task (id)") == ""


def test_upsert_and_ddl_phrases_not_taken_as_table(db):
    """upsert / DDL / 冲突子句里的关键字不能当表名（曾把 SET 当表名拒掉 upsert）。"""
    for sql in (
        "INSERT INTO plugin_system_subagent_task (id) VALUES (?) "
        "ON CONFLICT(id) DO UPDATE SET id=excluded.id, status=excluded.status",
        "INSERT OR REPLACE INTO plugin_system_subagent_task (id) VALUES (?)",
        "UPDATE OR IGNORE plugin_system_subagent_task SET status=? WHERE task_id=?",
        "UPDATE OR REPLACE plugin_system_subagent_task SET status=?",
        "DROP TABLE IF EXISTS plugin_system_subagent_task",
        "CREATE TABLE IF NOT EXISTS plugin_system_subagent_task (id TEXT)",
    ):
        assert db.check_sql(sql) == "", sql


def test_conflict_clause_does_not_bypass_prefix(db):
    """跳过冲突子句后仍要校验真表名，不能因此放过主程序表。"""
    for sql in (
        "UPDATE OR IGNORE sessions SET status=?",
        "UPDATE OR REPLACE messages SET body=?",
        "DROP TABLE IF EXISTS project_notes",
        "INSERT OR REPLACE INTO sessions (id) VALUES (?)",
    ):
        assert db.check_sql(sql) != "", sql


def test_main_program_table_rejected(db):
    for sql in (
        "SELECT * FROM sessions",
        "INSERT INTO messages (id) VALUES (?)",
        "DELETE FROM key_documents",
        "DROP TABLE plugin_other_task",
    ):
        assert db.check_sql(sql), sql


def test_leading_subquery_and_join_rejected(db):
    """子查询/关联里的主程序表也要拦（正则扫全语句而非只看首表）。"""
    sql = "SELECT * FROM plugin_system_subagent_task t JOIN sessions s ON s.id = t.id"
    assert "sessions" in db.check_sql(sql)


def test_system_table_rejected(db):
    assert db.check_sql("SELECT name FROM sqlite_master") != ""
    assert db.check_sql("SELECT * FROM sqlite_sequence") != ""


def test_forbidden_statements_rejected(db):
    for sql in (
        "PRAGMA page_size",
        "ATTACH DATABASE 'x.db' AS x",
        "DETACH DATABASE x",
        "VACUUM",
        "SELECT load_extension('evil')",
    ):
        assert db.check_sql(sql), sql


def test_forbidden_verb_as_first_token_rejected(db):
    assert db.check_sql("REINDEX plugin_system_subagent_task") != ""
    assert db.check_sql("EXPLAIN SELECT 1") != ""


def test_multi_statement_injection_rejected(db):
    sql = "DELETE FROM plugin_system_subagent_task; DROP TABLE sessions"
    assert db.check_sql(sql) != ""


def test_empty_sql_rejected(db):
    assert db.check_sql("") == "空语句"
    assert db.check_sql("   ") == "空语句"


def test_execute_rejects_before_hitting_db(db, monkeypatch):
    calls = []

    class _FakeDB:
        def execute_sql(self, sql, params=()):
            calls.append((sql, params))
            return True, []

    monkeypatch.setattr("app.utils.db_manager.DatabaseManager", _FakeDB)

    ok, result = db.execute("SELECT * FROM sessions")

    assert ok is False
    assert "前缀" in result
    assert calls == []


def test_execute_passes_through_and_normalizes_params(db, monkeypatch):
    calls = []

    class _FakeDB:
        def execute_sql(self, sql, params=()):
            calls.append((sql, params))
            return True, [{"id": 1}]

    monkeypatch.setattr("app.utils.db_manager.DatabaseManager", _FakeDB)

    ok, rows = db.execute("SELECT * FROM plugin_system_subagent_task WHERE id=?", [1])

    assert ok is True
    assert rows == [{"id": 1}]
    assert calls and calls[0][1] == (1,)


def test_query_swallows_failure(db, monkeypatch):
    class _FakeDB:
        def execute_sql(self, sql, params=()):
            return False, "boom"

    monkeypatch.setattr("app.utils.db_manager.DatabaseManager", _FakeDB)

    assert db.query("SELECT * FROM plugin_system_subagent_task") == []
