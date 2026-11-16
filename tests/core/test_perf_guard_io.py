# -*- coding: utf-8 -*-
"""I/O 域行为等价护栏测试（批次 3 性能优化前置）。

锁住的是「优化前后都应逐字段/逐字节一致」的输出契约，不是性能数字。
本文件禁止出现 DDL 次数、commit 次数、b64decode 次数这类计数断言；
那类由批次 3 修复者随修复以 TDD 方式写入，避免当前工作树出现预期红用例。

覆盖：
- SubAgentLogRepository / ProjectWorkdirRepository 建表迁移幂等
- SessionRepository 的 extras 全删全插语义、主 blob 与 extras 合并一致性
- freelist 回收开关不改变会话内容
- vision data:image 落盘 → 引用块 → 回读字节级往返
- DatabaseManager 攒批 / flush / needs_clean_tx 下的数据一致性
"""

from __future__ import annotations

import base64
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture()
def store(tmp_path):
    """临时 SQLite 的 SessionStore（单例逐例重建，避免跨例串库）。"""
    from app.core.store.session_store import SessionStore
    from app.utils.db_manager import DatabaseManager

    SessionStore._instance = None
    DatabaseManager._instance = None
    s = SessionStore(str(tmp_path / "db"))
    assert s.is_initialized, "SessionStore 应初始化成功"
    yield s
    try:
        s.close()
    except Exception:
        pass
    SessionStore._instance = None
    DatabaseManager._instance = None


@pytest.fixture(autouse=True)
def reset_legacy_flag():
    """project_workdir 迁移为进程内一次性标记，测试间必须重置。"""
    from app.core.store.project_workdir_repository import ProjectWorkdirRepository

    ProjectWorkdirRepository._legacy_synced = False
    yield
    ProjectWorkdirRepository._legacy_synced = False


def _session(session_id: str, messages: list) -> Dict[str, Any]:
    return {
        "session_id": session_id,
        "title": "护栏会话",
        "project": "默认项目",
        "messages": messages,
        "system_prompt": "sys",
        "compaction_state": {"round": 2},
        "compaction_cache": {"hit": True},
        "message_count": len(messages),
        "user_edited_title": False,
        "worktree_path": "D:/work/DriFox",
        "preview": "预览",
        "context_usage": 128,
        "last_api_prompt_tokens": 64,
        "last_api_message_count": len(messages),
        "last_api_prompt_from_usage": None,
    }


def _msg(role: str, content: str, **extra):
    m = {"role": role, "content": content, "timestamp": "2026-01-01 00:00:00"}
    m.update(extra)
    return m


def _fresh_db(tmp_path: Path):
    """独立 DatabaseManager 实例（绕开单例，直接连临时文件）。"""
    from app.utils.db_manager import DatabaseManager

    db = object.__new__(DatabaseManager)
    db._conn = None
    db._db_path = None
    db._read_lock = __import__("threading").Lock()
    db._write_lock = __import__("threading").Lock()
    db._wal_mode_enabled = False
    db._pending_commit = False
    db._commit_timer = None
    assert db.connect(str(tmp_path / "fresh.db")) is True
    return db


# ── 一、重复 DDL / 必抛 ALTER 的幂等行为 ──

def test_subagent_log_ensure_table_repeated_keeps_task_fields(store):
    """锁住：ALTER 已存在列必抛异常被吞掉后，任务 CRUD 与序列化产物不变。"""
    from app.core.store.subagent_log_repository import SubAgentLogRepository

    repo = SubAgentLogRepository(store._db)
    logs = [{"t": 1, "text": "开始"}, {"t": 2, "text": "结束"}]
    summary = {"steps": 2, "ok": True}
    assert repo._ensure_table() is True
    assert repo.save_task("task-1", "build", "写护栏", status="running",
                          logs=logs, summary=summary, session_id="sess-1") is True
    for _ in range(3):
        assert repo._ensure_table() is True  # 重复 ALTER 走 duplicate column 分支

    got = repo.get_task("task-1")
    assert got["task_id"] == "task-1"
    assert got["agent_name"] == "build"
    assert got["task_description"] == "写护栏"
    assert got["session_id"] == "sess-1"
    assert got["status"] == "running"
    assert got["logs"] == logs, "logs 经 serde 往返后必须逐项一致"
    assert got["summary"] == summary, "summary 经 serde 往返后必须逐项一致"

    # 更新路径不丢 session_id / 不改序列化形态
    assert repo.update_status("task-1", "finished", result="done",
                              logs=[{"t": 3, "text": "收尾"}], summary={"steps": 3}) is True
    got2 = repo.get_task("task-1")
    assert got2["session_id"] == "sess-1"
    assert got2["status"] == "finished"
    assert got2["result"] == "done"
    assert got2["logs"] == [{"t": 3, "text": "收尾"}]
    assert got2["summary"] == {"steps": 3}
    assert repo.delete_task("task-1") is True
    assert repo.get_task("task-1") is None


def test_project_workdir_ensure_table_repeated_keeps_mapping(store):
    """锁住：重复 ensure_table 不覆盖已写入路径，list_all / get 逐字段一致。"""
    from app.core.store.project_workdir_repository import ProjectWorkdirRepository

    repo = ProjectWorkdirRepository(store._db)
    assert repo.ensure_table() is True
    assert repo.set("项目甲", r"D:\work\a") is True
    assert repo.set("项目乙", "D:/work/b") is True
    for _ in range(3):
        assert repo.ensure_table() is True

    assert repo.get("项目甲") == "D:/work/a", "反斜杠归一化是既有输出契约"
    assert repo.get("项目乙") == "D:/work/b"
    assert repo.list_all() == {"项目甲": "D:/work/a", "项目乙": "D:/work/b"}
    assert repo.get("不存在") is None
    assert repo.clear("项目乙") is True
    assert repo.get("项目乙") is None
    assert repo.list_all() == {"项目甲": "D:/work/a"}


def test_project_workdir_legacy_migration_conflict_keeps_new_value(tmp_path):
    """锁住：老 key_documents 迁移 ON CONFLICT DO NOTHING，新表值优先不回退。"""
    from app.core.store.project_workdir_repository import ProjectWorkdirRepository

    db = _fresh_db(tmp_path)
    try:
        db.execute_sql(
            "CREATE TABLE key_documents ("
            "id TEXT PRIMARY KEY, project TEXT NOT NULL, file_path TEXT NOT NULL, "
            "is_working_dir INTEGER DEFAULT 0)"
        )
        db.execute_sql(
            "INSERT INTO key_documents VALUES (?, ?, ?, ?)",
            ("k1", "老项目", r"D:\legacy\path", 1),
        )
        db.flush()
        repo = ProjectWorkdirRepository(db)
        assert repo.ensure_table() is True
        assert repo.get("老项目") == "D:/legacy/path"

        assert repo.set("老项目", "D:/user/edited") is True
        assert repo.ensure_table() is True  # 重跑迁移不得把用户改动覆盖回老值
        assert repo.get("老项目") == "D:/user/edited"
    finally:
        db.close()


# ── 二、_write_extras / 主 blob 一致性 / freelist 回收 ──

def test_write_extras_full_replace_semantics_and_blob_merge(store):
    """锁住：_write_extras 全删全插语义（旧字段消失、新字段生效），主 blob 与 extras 合并后逐字段还原。"""
    from app.core.store.serde import serialize

    repo = store._session_repo
    extras_v1 = {
        0: {"reasoning_content": serialize("推理一"), "diff": serialize("diff-1")},
        1: {"arguments": serialize({"x": 1})},
    }
    repo._write_extras("s-extras", extras_v1)
    assert repo.load_extras_for_session("s-extras") == {
        0: {"reasoning_content": "推理一", "diff": "diff-1"},
        1: {"arguments": {"x": 1}},
    }, "写入 → 读回必须逐字段一致（serde 透明解压）"

    # 全删全插：第二次只留一个新字段，旧的行必须整体消失
    extras_v2 = {2: {"reasoning_content": serialize("推理二")}}
    repo._write_extras("s-extras", extras_v2)
    assert repo.load_extras_for_session("s-extras") == {2: {"reasoning_content": "推理二"}}
    assert repo.load_extras_for_session("s-extras", [0, 1]) == {}, "按 idx 查询同样不得残留旧行"

    # 空 dict 等价于清空，且不影响其他会话
    repo._write_extras("s-other", {5: {"diff": serialize("other")}})
    repo._write_extras("s-extras", {})
    assert repo.load_extras_for_session("s-extras") == {}
    assert repo.load_extras_for_session("s-other") == {5: {"diff": "other"}}


def test_save_session_extras_match_primary_blob(store):
    """锁住：保活窗外三字段剥离 + extras 合并后，get_full_messages 逐字段等于原始消息。"""
    messages = [
        _msg("assistant", "a0", reasoning_content="思考0"),
        _msg("tool", "r0", arguments={"x": 0}, diff="diff-0"),
        _msg("assistant", "a1", reasoning_content="思考1"),
        _msg("tool", "r1", arguments={"x": 1}, diff="diff-1"),
        _msg("assistant", "a2", reasoning_content="思考2"),
        _msg("tool", "r2", arguments={"x": 2}, diff="diff-2"),
        _msg("user", "u3"),
    ]
    original = [dict(m) for m in messages]
    assert store.save_session(_session("s-blob", messages)) is True

    light = store.get_session("s-blob")["messages"]
    assert "reasoning_content" not in light[0], "保活窗外字段应剥离"
    assert light[0].get("_x_idx") == 0
    for m in light[-3:]:
        assert "_x_idx" not in m, "保活窗内字段原样留在主 blob"

    merged = store.get_full_messages("s-blob")
    # _x_idx 是轻量 blob 的形态标记，合并后其余业务字段必须与原始逐项相等
    assert len(merged) == len(original)
    for got, want in zip(merged, original):
        assert {k: v for k, v in got.items() if k != "_x_idx"} == want
    assert messages == original, "save 不得污染内存事实源"


def test_reclaim_freelist_does_not_change_session_payload(store, monkeypatch):
    """锁住：freelist 阈值/回收开关前后，会话字段与消息字节必须完全一致。"""
    from app.core.store.serde import deserialize

    repo = store._session_repo
    payload = _session("s-vac", [
        _msg("user", "问题"),
        _msg("assistant", "回答", reasoning_content="内部推理"),
    ])
    assert store.save_session(payload) is True

    ok, rows = store._db.execute_sql("SELECT messages FROM sessions WHERE session_id = 's-vac'")
    assert ok and rows
    blob_before = bytes(rows[0]["messages"])

    # 无论阈值取什么（触发或不触发 incremental_vacuum），数据都不得变
    for threshold in (0, 5000, 10**9):
        repo._reclaim_freelist_if_needed(threshold_pages=threshold, reclaim_pages=10)

    ok2, rows2 = store._db.execute_sql("SELECT messages FROM sessions WHERE session_id = 's-vac'")
    assert ok2 and rows2
    assert bytes(rows2[0]["messages"]) == blob_before, "回收空闲页不得改写主 blob 一个字节"
    got = store.get_session("s-vac")
    assert got["title"] == "护栏会话"
    assert got["project"] == "默认项目"
    assert got["system_prompt"] == "sys"
    assert got["compaction_state"] == {"round": 2}
    assert got["compaction_cache"] == {"hit": True}
    assert deserialize(blob_before)[0]["content"] == "问题"
    assert store.get_full_messages("s-vac")[1]["reasoning_content"] == "内部推理"


# ── 三、vision 块落盘 / 回读往返 ──

def _png_bytes() -> bytes:
    """最小合法 PNG（1x1），避免依赖 Qt 图像构造。"""
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )


def test_vision_persist_revive_is_byte_identical(tmp_path):
    """锁住：data:image → image_ref → 回读的 data URI 解码后与原始 PNG 逐字节相同。"""
    import json

    from app.core.store.session_repository import (
        _persist_vision_image_blocks_impl,
        revive_vision_image_refs,
    )

    sid = "vision-guard"
    png = _png_bytes()
    uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    messages = [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": uri}},
            {"type": "text", "text": "看图"},
        ],
    }]

    _persist_vision_image_blocks_impl(messages, sid, tmp_path)
    ref = messages[0]["content"][0]
    assert ref["type"] == "image_ref"
    assert ref["image_ref"]["session_id"] == sid
    assert ref["image_ref"]["meta"] == "image/png;base64"
    persisted = tmp_path / "screenshots" / "persisted" / sid
    files = list(persisted.iterdir())
    assert len(files) == 1 and files[0].read_bytes() == png, "落盘文件字节必须等于原图"
    assert messages[0]["content"][1] == {"type": "text", "text": "看图"}, "非图片块原样保留"

    # 二次 persist 幂等：引用块逐字段不变、不重复落盘
    snapshot = json.dumps(ref, sort_keys=True)
    _persist_vision_image_blocks_impl(messages, sid, tmp_path)
    assert json.dumps(messages[0]["content"][0], sort_keys=True) == snapshot
    assert len(list(persisted.iterdir())) == 1

    revived = revive_vision_image_refs([json.loads(json.dumps(messages[0]))], tmp_path)[0]
    block = revived["content"][0]
    assert block["type"] == "image_url"
    got_uri = block["image_url"]["url"]
    assert got_uri.startswith("data:image/png;base64,")
    assert base64.b64decode(got_uri.split("base64,", 1)[1]) == png, "往返后必须逐字节还原"
    assert revived["content"][1] == {"type": "text", "text": "看图"}


def test_vision_missing_file_keeps_degraded_contract(tmp_path):
    """锁住：引用文件缺失时降级为文字占位块，不抛错、不丢整条消息。"""
    from app.core.store.session_repository import revive_vision_image_refs

    messages = [{
        "role": "user",
        "content": [
            {"type": "image_ref", "image_ref": {"file": "gone.png", "meta": "image/png", "session_id": "nope"}},
            {"type": "text", "text": "正文仍在"},
        ],
    }]
    out = revive_vision_image_refs(messages, tmp_path)
    block = out[0]["content"][0]
    assert block["type"] == "text"
    assert "图片已失效" in block["text"] and "gone.png" in block["text"]
    assert out[0]["content"][1] == {"type": "text", "text": "正文仍在"}


# ── 四、db_manager 攒批 / flush / needs_clean_tx 的数据一致性 ──

def test_db_manager_deferred_writes_are_identical_after_flush(tmp_path):
    """锁住：攒批窗口内多次 DML 与 flush 后，独立连接读到的行逐字段一致。"""
    db = _fresh_db(tmp_path)
    try:
        db.execute_sql("CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT)")
        assert db.execute_sql("INSERT INTO t VALUES (?, ?)", ("a", "甲"))[0] is True
        assert db.execute_sql("INSERT INTO t VALUES (?, ?)", ("b", "乙"))[0] is True
        assert db.execute_sql("UPDATE t SET v = ? WHERE k = ?", ("甲改", "a"))[0] is True

        # 同连接可读到自身挂起写（既有语义）
        ok, rows = db.execute_sql("SELECT k, v FROM t ORDER BY k")
        assert ok and [dict(r) for r in rows] == [{"k": "a", "v": "甲改"}, {"k": "b", "v": "乙"}]

        assert db.flush() is True
        con = sqlite3.connect(str(tmp_path / "fresh.db"))
        try:
            got = con.execute("SELECT k, v FROM t ORDER BY k").fetchall()
        finally:
            con.close()
        assert got == [("a", "甲改"), ("b", "乙")], "flush 后独立连接必须读到同一份数据"
    finally:
        db.close()


def test_db_manager_flush_and_backup_preserve_rows(tmp_path):
    """锁住：flush 幂等、backup_to 前自动 flush，备份库逐行等于源库。"""
    db = _fresh_db(tmp_path)
    try:
        db.execute_sql("CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT)")
        db.execute_sql("INSERT INTO t VALUES (?, ?)", ("k1", "值1"))
        db.execute_sql("INSERT INTO t VALUES (?, ?)", ("k2", "值2"))
        assert db.flush() is True
        assert db.flush() is True  # 幂等，无 pending 时为空操作

        target = str(tmp_path / "backup.db")
        ok, msg = db.backup_to(target)
        assert ok is True, msg
        con = sqlite3.connect(target)
        try:
            assert con.execute("SELECT k, v FROM t ORDER BY k").fetchall() == [("k1", "值1"), ("k2", "值2")]
        finally:
            con.close()
    finally:
        db.close()


def test_db_manager_vacuum_path_keeps_rows_and_statements(tmp_path):
    """锁住：needs_clean_tx 类语句执行前后，已写入数据不丢、DDL 不残留半状态。"""
    db = _fresh_db(tmp_path)
    try:
        db.execute_sql("CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT)")
        db.execute_sql("INSERT INTO t VALUES (?, ?)", ("k1", "值1"))
        # PRAGMA incremental_vacuum 触发 execute_sql 的 clean_tx 前置 flush 分支
        db.execute_sql("PRAGMA incremental_vacuum(1)")
        assert db.flush() is True

        ok, rows = db.execute_sql("SELECT k, v FROM t")
        assert ok and [dict(r) for r in rows] == [{"k": "k1", "v": "值1"}]
        assert db.execute_sql("DELETE FROM t WHERE k = ?", ("k1",))[0] is True
        assert db.flush() is True
        ok2, rows2 = db.execute_sql("SELECT k, v FROM t")
        assert ok2 and list(rows2) == []
    finally:
        db.close()
