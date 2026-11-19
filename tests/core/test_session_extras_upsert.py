# -*- coding: utf-8 -*-
"""[E10] _write_extras 全删全插 → (sid, msg_idx) 差异 upsert 的红线回归。

方案取舍（两候选）：
- A（选定）：内存行集缓存 `_extras_written[sid][(idx, field)] = value_bytes` 与
  本次 extract 产物 diff——新增/变更行 executemany INSERT OR REPLACE，消失行
  executemany 精确 DELETE；缓存缺失（重启首拍/写失败后）退化为旧全删全插路径
  重建基线。无变化拍 0 SQL。
- B（否决）：每次 save 先 SELECT DB 现行集再 diff——每拍多一次全表读+数千行
  bytes 拷贝，流式拍成本不达标（E3 口径 write_extras 104ms@16K，读不比写省）。

等价性契约：diff 路径的 DB 最终状态必须与全删全插严格一致（旧行删/新行插/
变行覆盖），既有门禁 test_write_extras_equiv.py 的读回断言全部继续成立。

R1 红线三场景：
1. 写→读→merge 往返一致（offload 大字段逐字节）
2. 崩溃模拟（多拍 save 后直接重开读库，不调 save）恢复完整
3. 会话删除后 extras 无孤儿行
附加护栏：截断删超界行、同 idx 变更覆盖、无变化拍零 SQL、批量失败降级后
缓存仍一致、未知基线全删全插自愈。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.store.serde import serialize  # noqa: E402
from app.core.store.session_store import SessionStore  # noqa: E402
from app.utils.db_manager import DatabaseManager  # noqa: E402

OFFLOAD_KEYS = ("reasoning_content", "arguments", "diff")


@pytest.fixture()
def store(tmp_path):
    """临时 SQLite 的 SessionStore（单例逐例重建，对齐 test_write_extras_equiv 口径）。"""
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


def _mk_msgs(n: int, big: bool = False) -> List[Dict[str, Any]]:
    """user/assistant 交替语料：assistant 带 offload 三字段（big 模式字段放大）。"""
    msgs: List[Dict[str, Any]] = []
    for i in range(n):
        if i % 2 == 0:
            msgs.append({"role": "user", "content": f"问题-{i} " + "上下文" * 20, "timestamp": f"t{i}"})
        else:
            pad = ("推" * 200 + f"-{i}") if big else (f"思考-{i}")
            msgs.append(
                {
                    "role": "assistant",
                    "content": f"回答-{i} " + "正文" * 30,
                    "reasoning_content": pad,
                    "arguments": {"tool": f"t{i}", "data": list(range(20))},
                    "diff": f"{'-' * (400 if big else 100)}-{i}",
                    "timestamp": f"t{i}",
                }
            )
    return msgs


def _make_session(sid: str, msgs: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"session_id": sid, "name": f"会话-{sid}", "project": "默认项目", "messages": msgs}


def _assert_roundtrip(repo, sid: str, origin: List[Dict[str, Any]]) -> None:
    """红线①：get_full_messages 恢复后逐条逐字段与原始一致。"""
    merged = repo.get_full_messages(sid)
    assert len(merged) == len(origin), f"条数不一致: {len(merged)} != {len(origin)}"
    for i, (src, got) in enumerate(zip(origin, merged)):
        assert got.get("role") == src.get("role"), f"msg[{i}] role 漂移"
        assert got.get("content") == src.get("content"), f"msg[{i}] content 漂移"
        for f in OFFLOAD_KEYS:
            assert got.get(f) == src.get(f), f"msg[{i}] 字段 {f} 恢复不一致（往返破坏）"


def test_roundtrip_multi_saves_byte_identical(store):
    """红线①：多拍 save（冷启动/追加/尾条变化）后写→读→merge 往返一致。"""
    repo = store._session_repo
    sid = "s-rt"
    msgs = _mk_msgs(40, big=True)
    repo.save(_make_session(sid, msgs))
    _assert_roundtrip(repo, sid, msgs)
    # 流式拍：尾条 partial 文本变化（extras 集合不变）
    msgs[-1]["content"] += " 流式增量"
    repo.save(_make_session(sid, msgs))
    _assert_roundtrip(repo, sid, msgs)
    # 追加拍：新消息滑出保活窗 → extras 增行
    msgs.extend(_mk_msgs(6)[len(msgs) % 2 :])
    repo.save(_make_session(sid, msgs))
    _assert_roundtrip(repo, sid, msgs)


def test_crash_recovery_without_save(store):
    """红线②：多拍 save 后模拟强退重开（直接读库），历史消息恢复完整。"""
    repo = store._session_repo
    sid = "s-crash"
    msgs = _mk_msgs(30, big=True)
    for extra in ("", " 拍1", " 拍2", " 拍3"):
        if extra:
            msgs[-1]["content"] += extra
        repo.save(_make_session(sid, [dict(m) for m in msgs]))
    # 不再调 save，直接从库读（等价进程崩溃后重开）
    merged = repo.get_full_messages(sid)
    assert len(merged) == len(msgs)
    for i, (src, got) in enumerate(zip(msgs, merged)):
        for f in OFFLOAD_KEYS:
            assert got.get(f) == src.get(f), f"崩溃窗口 msg[{i}] 字段 {f} 丢失"


def test_no_orphan_rows_after_delete(store):
    """红线③：save 后 delete，session_msg_extras 无孤儿行。"""
    repo = store._session_repo
    sid = "s-del"
    msgs = _mk_msgs(20)
    repo.save(_make_session(sid, msgs))
    ok, rows = repo._execute("SELECT COUNT(*) AS n FROM session_msg_extras WHERE session_id = ?", (sid,))
    assert rows[0]["n"] > 0, "前置：save 后应有 extras 行"
    assert repo.delete(sid), "delete 应成功"
    ok, rows = repo._execute("SELECT COUNT(*) AS n FROM session_msg_extras WHERE session_id = ?", (sid,))
    assert rows[0]["n"] == 0, "删除后 extras 存在孤儿行"


def test_truncate_removes_out_of_range_rows(store):
    """截断场景：消息列表变短后 save，超界 extras 行被删，idx 复用不误命中旧字段。"""
    repo = store._session_repo
    sid = "s-trunc"
    msgs = _mk_msgs(30)
    repo.save(_make_session(sid, msgs))
    ok, rows = repo._execute("SELECT COUNT(*) AS n FROM session_msg_extras WHERE session_id = ?", (sid,))
    assert rows[0]["n"] > 0
    # 截断到 10 条（窗外 idx<7：带字段的是 assistant idx 1,3,5）
    truncated = msgs[:10]
    repo.save(_make_session(sid, truncated))
    ok, rows = repo._execute("SELECT COUNT(*) AS n FROM session_msg_extras WHERE session_id = ?", (sid,))
    expected = sum(1 for i in range(7) if any(truncated[i].get(f) for f in OFFLOAD_KEYS)) * len(OFFLOAD_KEYS)
    assert rows[0]["n"] == expected, f"截断后 extras 应恰为窗外语料行数 {expected}，实得 {rows[0]['n']}"
    ok, rows = repo._execute("SELECT DISTINCT msg_idx FROM session_msg_extras WHERE session_id = ?", (sid,))
    assert all(r["msg_idx"] < 10 for r in rows), "截断后不得残留超界行（idx 复用会串字段）"
    merged = repo.get_full_messages(sid)
    for i, m in enumerate(merged):
        for f in OFFLOAD_KEYS:
            assert m.get(f, None) == truncated[i].get(f, None), f"截断后 msg[{i}] 字段串扰"


def test_same_idx_value_change_upserts(store):
    """compaction 漂移：同 idx 内容变化 → diff 判定变更 → 覆盖写入新值。"""
    repo = store._session_repo
    sid = "s-drift"
    msgs = _mk_msgs(12)
    repo.save(_make_session(sid, msgs))
    # 就地改窗外消息（同 idx 不同值）；同时变尾条触发 save 全链
    # （既有内容指纹只看 (len, 尾条 hash)，窗外改动本身不落库——旧实现同样如此）
    msgs[0] = {**msgs[0], "content": "问题-0（重写）"}
    msgs[2] = {**msgs[2], "reasoning_content": "重写后的思考"}
    msgs[-1]["content"] += " 触发落库"
    repo.save(_make_session(sid, msgs))
    _assert_roundtrip(repo, sid, msgs)


def test_unchanged_extras_zero_write_sql(store):
    """无变化拍：extras 集合不变时 _write_extras 零写 SQL（流式拍降本核心）。"""
    repo = store._session_repo
    sid = "s-zero"
    msgs = _mk_msgs(24)
    repo.save(_make_session(sid, msgs))  # 冷拍建基线
    stmts: list[str] = []
    conn = repo._db._conn
    conn.set_trace_callback(stmts.append)
    try:
        # 流式拍：仅尾条文本变化（extras 集合不变）
        msgs[-1]["content"] += " 流式增量"
        repo.save(_make_session(sid, msgs))
    finally:
        conn.set_trace_callback(None)
    writes = [q for q in stmts if "session_msg_extras" in q and ("INSERT" in q or "DELETE" in q)]
    assert writes == [], f"无变化拍不应有 extras 写 SQL，实得: {writes}"
    _assert_roundtrip(repo, sid, msgs)


def test_unknown_baseline_falls_back_to_full_rewrite(store):
    """未知基线（缓存缺失，模拟重启首拍）：退化为全删全插，数据正确并重建基线。"""
    repo = store._session_repo
    sid = "s-cold"
    msgs = _mk_msgs(16)
    repo.save(_make_session(sid, msgs))
    repo._extras_written.pop(sid, None)  # 模拟重启后缓存为空
    msgs[-1]["content"] += " 重启后拍"
    repo.save(_make_session(sid, msgs))
    # 先断基线重建（get_full_messages 会经 get() 按设计失效基线，勿先调）
    assert sid in repo._extras_written, "全删全插后应重建基线缓存"
    _assert_roundtrip(repo, sid, msgs)


def test_batch_failure_falls_back_cache_consistent(store, monkeypatch):
    """批量失败降级逐条：读回一致且基线缓存与 DB 一致（不残留脏缓存）。"""
    repo = store._session_repo
    sid = "s-fallback"
    msgs = _mk_msgs(10)
    repo.save(_make_session(sid, msgs))  # 建基线
    # 第二拍：改动 extras（追加消息滑出窗）并让 executemany 首次调用抛错
    # （sqlite3.Cursor 不可 patch，沿用既有门禁的代理 db 手法）
    real_db = repo._db
    conn = real_db._conn
    calls = {"n": 0}

    class FlakyCursor:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, *a, **k):
            return self._c.execute(sql, *a, **k)

        def executemany(self, sql, *a, **k):
            if "session_msg_extras" in sql:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise sqlite3.OperationalError("injected failure")
            return self._c.executemany(sql, *a, **k)

        def __getattr__(self, n):
            return getattr(self._c, n)

    class FlakyConn:
        def cursor(self, *a, **k):
            return FlakyCursor(conn.cursor(*a, **k))

        def __getattr__(self, n):
            return getattr(conn, n)

    class ProxyDB:
        _write_lock = real_db._write_lock
        _conn = FlakyConn()

        def _schedule_commit(self):
            return real_db._schedule_commit()

        def __getattr__(self, n):
            return getattr(real_db, n)

    monkeypatch.setattr(repo, "_db", ProxyDB())
    try:
        msgs.extend(_mk_msgs(4)[0:])
        repo.save(_make_session(sid, msgs))
    finally:
        monkeypatch.undo()
    assert calls["n"] >= 1, "前置：注入的批量失败应至少命中一次"
    # 缓存与 DB 一致：再拍一次无变化 → 零写 SQL
    # （先于读回验证：get_full_messages 经 get() 会按设计失效基线，打乱零 SQL 前提）
    stmts: list[str] = []
    std_conn = repo._db._conn
    std_conn.set_trace_callback(stmts.append)
    try:
        repo.save(_make_session(sid, msgs))
    finally:
        std_conn.set_trace_callback(None)
    writes = [q for q in stmts if "session_msg_extras" in q and ("INSERT" in q or "DELETE" in q)]
    assert writes == [], "降级成功后基线缓存应与 DB 一致（下一拍零写 SQL）"
    _assert_roundtrip(repo, sid, msgs)


def test_diff_final_state_equals_full_rewrite(store):
    """等价性契约：diff 路径最终 DB 状态与全删全插严格一致（含部分消失场景）。"""
    repo = store._session_repo
    e1 = {0: {"reasoning_content": serialize("R0"), "diff": serialize("D0")}, 1: {"arguments": serialize({"a": 1})}}
    e2 = {2: {"reasoning_content": serialize("R2")}}
    repo._write_extras("s-eq-a", e1)
    repo._write_extras("s-eq-a", e2)  # diff 路径（有基线）
    repo._write_extras("s-eq-b", e1)
    repo._write_extras("s-eq-b", e2)
    repo._extras_written.pop("s-eq-b", None)
    repo._write_extras("s-eq-b", e2)  # 全删全插路径（未知基线）
    got_diff = repo.load_extras_for_session("s-eq-a")
    got_full = repo.load_extras_for_session("s-eq-b")
    assert (
        got_diff
        == got_full
        == {
            2: {"reasoning_content": "R2"},
        }
    ), "diff 与全删全插最终状态必须一致"
    # 二次相同写入：无变化拍后状态不变
    repo._write_extras("s-eq-a", e2)
    assert repo.load_extras_for_session("s-eq-a") == got_diff
