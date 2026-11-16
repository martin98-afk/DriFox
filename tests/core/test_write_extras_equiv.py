# -*- coding: utf-8 -*-
"""子任务 #23 行为等价回归：_write_extras 逐条 INSERT → 一次 executemany。

断言口径（沿用 #13 的「改前快照 + 内嵌 sha256 双断言」手法）：
1. 参考实现：在内存态复刻改动前的 DELETE + 逐条 INSERT，同组语料下与产品
   _write_extras 的读回结果逐字段一致；
2. 改前快照：内嵌改动前 (读回内容, 二次全删全插后内容) 的 sha256 前 16 位，
   保证输出字节零变化；
3. SQL 次数：改动前 _execute 调用数 = 1 DELETE + N INSERT，改动后 = 1 DELETE
   （批量 INSERT 走 cursor.executemany，不再经过 _execute）；
4. 契约护栏：None/{} 语义、按 idx 查询、会话隔离、全删全插旧字段消失、
   批量失败降级逐条后读回仍一致。

不依赖外部快照文件，便于后续规模化修复直接复用为门禁用例。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.store.serde import serialize  # noqa: E402
from app.core.store.session_store import SessionStore  # noqa: E402
from app.utils.db_manager import DatabaseManager  # noqa: E402

# 改动前（HEAD 6f191cddb 工作树）留档快照：由 .drifox/tmp/rollback/
# gen_w2_p03a_before.py 生成，值为 (读回+二次替换整体 sha256 前 16 位,
# 改动前 _execute 调用数)。内嵌以保证测试自包含。
_BEFORE_BASELINE = {
    "small": ("a3415b121a25ede4", 4),
    "many_120x3": ("395608da5d7e06f9", 361),
    "many_partial_idxs": ("6009db720f1099fd", 361),
    "empty_dict": ("67431eba9b2c5631", 1),
}


@pytest.fixture()
def store(tmp_path):
    """临时 SQLite 的 SessionStore（单例逐例重建，对齐 #11 护栏口径）。"""
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


def build_corpus():
    """返回 [(name, session_id, extras, 读回 idxs)]，extras 值为 serialize 产物。"""
    small = {
        0: {"reasoning_content": serialize("推理一"), "diff": serialize("diff-1")},
        1: {"arguments": serialize({"x": 1})},
    }
    many = {
        i: {
            "reasoning_content": serialize(f"推理-{i}"),
            "diff": serialize(f"diff-{i}"),
            "arguments": serialize({"idx": i}),
        }
        for i in range(120)
    }
    return [
        ("small", "s-small", small, None),
        ("many_120x3", "s-many", many, None),
        ("many_partial_idxs", "s-many", many, [0, 5, 119]),
        ("empty_dict", "s-empty", {}, None),
    ]


def _ref_write_extras(repo, session_id, extras, sql=None):
    """改动前参考实现：DELETE 全删 + 逐条 _execute(INSERT OR REPLACE)。"""
    if sql is None:
        sql = (
            "INSERT OR REPLACE INTO session_msg_extras (session_id, msg_idx, field, value) "
            "VALUES (?, ?, ?, ?)"
        )
    repo._execute("DELETE FROM session_msg_extras WHERE session_id = ?", (session_id,))
    if not extras:
        return
    for i, fields in extras.items():
        for f, v in fields.items():
            repo._execute(sql, (session_id, i, f, v))


def _snapshot(repo, sid, extras, idxs):
    """一组语料的完整可观测输出：写入读回 + 二次全删全插后读回。"""
    calls = {"n": 0}
    orig = repo._execute

    def counting(sql, params=(), _orig=orig, _c=calls):
        _c["n"] += 1
        return _orig(sql, params)

    repo._execute = counting
    try:
        repo._write_extras(sid, extras)
    finally:
        repo._execute = orig

    read = repo.load_extras_for_session(sid, idxs)
    repo._write_extras(sid, {2: {"reasoning_content": serialize("推理二")}})
    after_replace = repo.load_extras_for_session(sid)
    payload = json.dumps(
        {"read": read, "after_replace": after_replace},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return {
        "execute_calls": calls["n"],
        "read": read,
        "after_replace": after_replace,
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
    }


@pytest.mark.parametrize("name,sid,extras,idxs", build_corpus(), ids=[c[0] for c in build_corpus()])
def test_readback_matches_pre_change_snapshot(store, name, sid, extras, idxs):
    """读回逐字段 + sha256 与改动前快照一致；_execute 次数降到 1（仅 DELETE）。"""
    repo = store._session_repo
    snap = _snapshot(repo, sid, extras, idxs)

    exp_sha, exp_calls = _BEFORE_BASELINE[name]
    assert snap["sha256"] == exp_sha, f"{name}: 输出字节由 {exp_sha} 变为 {snap['sha256']}"
    assert snap["execute_calls"] == 1, (
        f"{name}: 改动前 _execute={exp_calls}，改动后应为 1（DELETE），实得 {snap['execute_calls']}"
    )
    # 二次全删全插语义：旧字段整体消失
    assert snap["after_replace"] == {2: {"reasoning_content": "推理二"}}


@pytest.mark.parametrize("name,sid,extras,idxs", build_corpus(), ids=[c[0] for c in build_corpus()])
def test_readback_matches_reference_impl(store, name, sid, extras, idxs):
    """与改动前参考实现逐字段一致（两个独立 session_id 隔离写入）。"""
    repo = store._session_repo
    _ref_write_extras(repo, "s-ref-" + sid, extras)
    repo._write_extras("s-new-" + sid, extras)
    ref = repo.load_extras_for_session("s-ref-" + sid, idxs)
    new = repo.load_extras_for_session("s-new-" + sid, idxs)
    assert new == ref, f"{name}: 与参考实现读回不一致"


def test_executemany_single_call_with_all_rows(store):
    """O(N)→O(1)：一次 cursor.executemany 下发全部参数行，行数与读回一致。"""
    repo = store._session_repo
    db = repo._db
    conn = db._conn
    extras = {
        i: {"reasoning_content": serialize(f"R{i}"), "diff": serialize(f"D{i}")}
        for i in range(120)
    }
    stmts: list[str] = []
    conn.set_trace_callback(stmts.append)
    try:
        repo._write_extras("s-batch", extras)
    finally:
        conn.set_trace_callback(None)
    inserts = [q for q in stmts if "INSERT OR REPLACE INTO session_msg_extras" in q]
    deletes = [q for q in stmts if "DELETE FROM session_msg_extras" in q]
    assert len(deletes) == 1, "DELETE 全删语义必须保留且仅一次"
    assert len(inserts) == 240, "SQLite 应展开 240 行 INSERT（120 idx × 2 字段）"
    read = repo.load_extras_for_session("s-batch")
    assert sum(len(v) for v in read.values()) == 240


def test_full_replace_and_session_isolation(store):
    """#11 护栏语义：旧字段消失、空 dict 清空、其他会话不受影响。"""
    repo = store._session_repo
    repo._write_extras("s-a", {
        0: {"reasoning_content": serialize("推理一"), "diff": serialize("diff-1")},
        1: {"arguments": serialize({"x": 1})},
    })
    assert repo.load_extras_for_session("s-a") == {
        0: {"reasoning_content": "推理一", "diff": "diff-1"},
        1: {"arguments": {"x": 1}},
    }
    repo._write_extras("s-a", {2: {"reasoning_content": serialize("推理二")}})
    assert repo.load_extras_for_session("s-a") == {2: {"reasoning_content": "推理二"}}
    assert repo.load_extras_for_session("s-a", [0, 1]) == {}, "按 idx 查询不得残留旧行"
    repo._write_extras("s-b", {5: {"diff": serialize("other")}})
    repo._write_extras("s-a", {})
    assert repo.load_extras_for_session("s-a") == {}
    assert repo.load_extras_for_session("s-b") == {5: {"diff": "other"}}


def test_load_extras_none_and_empty_contract(store):
    """None/{} 契约：未初始化或空 session_id 返回 None；无 extras 返回 {}。"""
    repo = store._session_repo
    assert repo.load_extras_for_session("") is None
    assert repo.load_extras_for_session("s-never-written") == {}
    assert repo.load_extras_for_session("s-never-written", [0, 1]) == {}


def test_batch_failure_falls_back_to_per_row(store, monkeypatch):
    """批量失败时回滚 SAVEPOINT 并降级逐条：读回仍与参考实现一致。

    sqlite3.Connection 不可猴子补丁，故替换 repo._db 为代理对象：其 _conn
    返回的代理连接只在 cursor().executemany 上抛错，其余方法透传真连接；
    _execute / _schedule_commit / _write_lock 同样透传，保证降级路径走真实 SQL。
    """
    repo = store._session_repo
    real_db = repo._db
    conn = real_db._conn
    extras = {
        i: {"reasoning_content": serialize(f"R{i}"), "diff": serialize(f"D{i}")}
        for i in range(30)
    }

    class FailingCursor:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, *a, **k):
            return self._c.execute(sql, *a, **k)

        def executemany(self, sql, *a, **k):
            raise sqlite3.OperationalError("simulated batch failure")

        def __getattr__(self, n):
            return getattr(self._c, n)

    class FailingConn:
        def cursor(self, *a, **k):
            return FailingCursor(conn.cursor(*a, **k))

        def __getattr__(self, n):
            return getattr(conn, n)

    class ProxyDB:
        _write_lock = real_db._write_lock
        _conn = FailingConn()

        def _schedule_commit(self):
            return real_db._schedule_commit()

        def __getattr__(self, n):
            return getattr(real_db, n)

    monkeypatch.setattr(repo, "_db", ProxyDB())
    try:
        repo._write_extras("s-fallback", extras)
    finally:
        monkeypatch.undo()

    assert repo.load_extras_for_session("s-fallback") is not None
    _ref_write_extras(repo, "s-fallback-ref", extras)
    assert repo.load_extras_for_session("s-fallback") == repo.load_extras_for_session("s-fallback-ref")


def test_save_session_extras_match_primary_blob(store):
    """save_session 全链路：主 blob + extras 合并后逐字段还原（#11 同名护栏口径）。"""
    def msg(role, content, **kw):
        m = {"role": role, "content": content, "timestamp": "2026-01-01 00:00:00"}
        m.update(kw)
        return m

    messages = [
        msg("assistant", "a0", reasoning_content="思考0"),
        msg("tool", "r0", arguments={"x": 0}, diff="diff-0"),
        msg("assistant", "a1", reasoning_content="思考1"),
        msg("tool", "r1", arguments={"x": 1}, diff="diff-1"),
        msg("assistant", "a2", reasoning_content="思考2"),
        msg("tool", "r2", arguments={"x": 2}, diff="diff-2"),
        msg("user", "u3"),
    ]
    original = [dict(m) for m in messages]
    payload: Dict[str, Any] = {
        "session_id": "s-blob", "title": "护栏会话", "project": "默认项目",
        "messages": messages, "system_prompt": "sys",
        "compaction_state": {"round": 2}, "compaction_cache": {"hit": True},
        "message_count": len(messages), "user_edited_title": False,
    }
    assert store.save_session(payload) is True
    merged = store.get_full_messages("s-blob")
    assert len(merged) == len(original)
    for got, want in zip(merged, original):
        assert {k: v for k, v in got.items() if k != "_x_idx"} == want
    assert messages == original, "save 不得污染内存事实源"
