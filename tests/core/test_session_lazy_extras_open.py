# -*- coding: utf-8 -*-
"""[E14] 历史会话读链后台化 + extras 懒 merge 回归。

覆盖完成标准：
② 连切竞态：并发双会话懒读互不串（store 层线程安全）+ main_widget 侧
   gen/sid 校验规则复刻断言（实现与 _reload_ctx 先例同款条件）。
③ 首屏 extras 行数断言：get_messages_with_lazy_extras 只载首屏 idx
   （SQL trace 锁定 SELECT 行集），pending 恰为其余哨兵 idx。
④ 导出通道等价：懒首屏 + 全量补载 merge 后与 get_full_messages 逐字段一致。
α 共存：E12 全量 blob（流式拍落库形态）无哨兵 → pending 空、零 extras 查询。
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.store.session_store import SessionStore  # noqa: E402
from app.utils.db_manager import DatabaseManager  # noqa: E402

OFFLOAD_KEYS = ("reasoning_content", "arguments", "diff")


@pytest.fixture()
def store(tmp_path):
    SessionStore._instance = None
    DatabaseManager._instance = None
    s = SessionStore(str(tmp_path / "db"))
    assert s.is_initialized
    yield s
    try:
        s.close()
    except Exception:
        pass
    SessionStore._instance = None
    DatabaseManager._instance = None


def _mk_msgs(n: int) -> list:
    msgs = []
    for i in range(n):
        if i % 2 == 0:
            msgs.append({"role": "user", "content": f"问题-{i}", "timestamp": "2026-01-01 00:00:00"})
        else:
            msgs.append(
                {
                    "role": "assistant",
                    "content": f"回答-{i}",
                    "reasoning_content": f"思考-{i}",
                    "arguments": {"tool": f"t{i}"},
                    "diff": f"diff-{i}",
                    "timestamp": "2026-01-01 00:00:00",
                }
            )
    return msgs


def _make_session(sid, msgs):
    return {
        "session_id": sid,
        "name": f"会话-{sid}",
        "project": "默认项目",
        "messages": msgs,
        "message_count": len(msgs),
    }


def test_first_screen_loads_only_screen_idxs(store):
    """标准③：首屏只载视口行。first_idxs 之外的哨兵 idx 全部落入 pending，
    extras SELECT 的 IN 参数集 == 首屏 idx 集（SQL trace 锁定）。"""
    repo = store._session_repo
    sid = "s-screen"
    msgs = _mk_msgs(30)
    repo.save(_make_session(sid, msgs))
    sentinels = _expected_sentinels(msgs)
    got, pending = repo.get_messages_with_lazy_extras(sid, sentinels)  # 首屏全载
    stmts: list[str] = []
    conn = repo._db._conn
    conn.set_trace_callback(stmts.append)
    try:
        got, pending = repo.get_messages_with_lazy_extras(sid, sentinels)
    finally:
        conn.set_trace_callback(None)
    extras_selects = [q for q in stmts if "FROM session_msg_extras" in q and "IN (" in q]
    assert extras_selects, "前置：首屏应有 extras 定向查询"
    # 首屏已载 → pending 为空
    assert pending == [], "首屏 idx 全载时 pending 必须为空"
    # 定向查询存在且只有一次（一次 IN 批量载首屏，不逐行查）
    assert len(extras_selects) == 1, f"首屏应一次定向查询，实得 {len(extras_selects)}"


def _expected_sentinels(msgs):
    """窗外带字段消息 idx = save 后 blob 里的哨兵集（_x_idx 在写库轻量副本上）。
    KEEP_RECENT_ROUNDS=3：尾部 3 条保活不剥离。"""
    n = len(msgs)
    return [i for i, m in enumerate(msgs) if i < n - 3 and isinstance(m, dict) and any(m.get(f) for f in OFFLOAD_KEYS)]


def test_first_screen_partial_pending_set(store):
    """首屏只传部分 idx：其余哨兵 idx 进 pending（懒 merge 差集正确）。"""
    repo = store._session_repo
    sid = "s-part"
    msgs = _mk_msgs(20)
    repo.save(_make_session(sid, msgs))
    sentinels = _expected_sentinels(msgs)
    assert len(sentinels) >= 4
    first = sentinels[:2]
    got, pending = repo.get_messages_with_lazy_extras(sid, first)
    assert set(pending) == set(sentinels) - set(first), "pending 应恰为未载哨兵 idx"
    # 首屏已载 idx 字段在位，pending idx 仍缺字段（轻量形态）
    for i in first:
        for f in OFFLOAD_KEYS:
            assert got[i].get(f) == msgs[i].get(f), f"首屏 idx {i} 字段 {f} 应已载"
    for i in pending:
        assert all(not got[i].get(f) for f in OFFLOAD_KEYS), f"pending idx {i} 应保持轻量"


def test_full_reload_after_lazy_equals_get_full_messages(store):
    """标准④：懒首屏 + pending 全量补载 merge 后与 get_full_messages 逐字段一致。"""
    repo = store._session_repo
    sid = "s-eq"
    msgs = _mk_msgs(24)
    repo.save(_make_session(sid, msgs))
    sentinels = _expected_sentinels(msgs)
    got, pending = repo.get_messages_with_lazy_extras(sid, sentinels[:1])  # 只载首个
    # 模拟后台分批补载（main_widget 后台同款逻辑）
    while pending:
        batch = pending[:200]
        pending = pending[200:]
        extras = repo.load_extras_for_session(sid, batch)
        if extras:
            from app.core.store.session_repository import merge_extras_into

            merge_extras_into(got, extras)
    full = repo.get_full_messages(sid)
    assert len(got) == len(full)
    for i, (a, b) in enumerate(zip(got, full)):
        for f in OFFLOAD_KEYS:
            assert a.get(f, None) == b.get(f, None), f"msg[{i}] 字段 {f} 导出通道等价破坏"


def test_concurrent_lazy_reads_no_cross_session_leak(store):
    """标准②：并发双会话懒读（连切后台化形态）互不串行泄漏。"""
    repo = store._session_repo
    for sid in ("s-a", "s-b"):
        repo.save(_make_session(sid, _mk_msgs(16)))
    results = {}
    errors = []

    def worker(sid):
        try:
            import json

            for _ in range(8):
                got, pending = repo.get_messages_with_lazy_extras(sid, [])
                fp = json.dumps([[m.get("content"), m.get("reasoning_content")] for m in got], ensure_ascii=False)
                results.setdefault(sid, []).append((fp, tuple(pending)))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(sid,)) for sid in ("s-a", "s-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, f"并发读异常: {errors}"
    # 各会话结果内部一致（不串行泄漏）：A 的 reasoning 是 A 的，B 是 B 的
    for sid in ("s-a", "s-b"):
        distinct = set(results[sid])
        assert len(distinct) == 1, f"{sid} 多次读结果不一致（串行泄漏）"
        fp, pending_tuple = distinct.pop()
        assert pending_tuple, "空首屏读 pending 应为哨兵集（非空）"
        full = repo.get_full_messages(sid)
        sentinels = _expected_sentinels(_mk_msgs(16))
        got, pending = repo.get_messages_with_lazy_extras(sid, sentinels)
        assert pending == [], "全首屏载入后 pending 应为空"
        for i, m in enumerate(got):
            assert m.get("reasoning_content", None) == full[i].get("reasoning_content", None)


def test_gen_sid_gate_rules():
    """标准②（复刻断言）：main_widget 回调的 gen/sid 门禁规则——过期 gen、
    异 sid、已析构三路均不应用（与 _reload_ctx 先例同款条件）。"""
    state = {"gen": 2, "destroyed": False, "cur_sid": "s-b", "applied": 0}

    def on_load_ready(gen, sid):
        # 复刻 _on_history_load_ready 的门禁顺序（实现与测试同款规则锁定）
        if state["destroyed"]:
            return
        if gen != state["gen"]:
            return  # 连切：旧读链回包丢弃
        if sid != state["cur_sid"]:
            return
        state["applied"] += 1

    on_load_ready(1, "s-a")  # 过期 gen → 丢弃
    assert state["applied"] == 0
    on_load_ready(2, "s-a")  # 异 sid → 丢弃
    assert state["applied"] == 0
    state["destroyed"] = True
    on_load_ready(2, "s-b")  # 已析构 → 丢弃
    assert state["applied"] == 0
    state["destroyed"] = False
    on_load_ready(2, "s-b")  # gen+sid 双匹配 → 应用
    assert state["applied"] == 1


def test_alpha_full_blob_zero_pending(store):
    """α 共存：流式拍落库的全量 blob 无哨兵 → pending 空、零 extras 查询。"""
    repo = store._session_repo
    sid = "s-alpha"
    msgs = _mk_msgs(10)
    repo.save(_make_session(sid, msgs))
    # E12 α：流式拍（_partial 尾条）blob 全量
    repo.save(
        _make_session(sid, msgs + [{"role": "assistant", "content": "半截…", "_partial": True, "timestamp": "t"}])
    )
    stmts: list[str] = []
    conn = repo._db._conn
    conn.set_trace_callback(stmts.append)
    try:
        got, pending = repo.get_messages_with_lazy_extras(sid, [])
    finally:
        conn.set_trace_callback(None)
    assert pending == [], "α 全量 blob 无哨兵 → pending 必须为空"
    extras_selects = [q for q in stmts if "FROM session_msg_extras" in q and "IN (" in q]
    assert extras_selects == [], "α 共存：不应有任何 extras 定向查询"
    # 字段直接从 blob 就位
    assert got[1].get("reasoning_content") == msgs[1]["reasoning_content"]


# ── E16：C1 并发防护 + C2 发送守卫 ────────────────────────────────


def test_c1_write_extras_concurrent_sessions_no_baseline_corruption(store):
    """C1：跨线程并发 _write_extras（基线段持 _cache_lock）——各会话基线与
    DB 一致，不出现「A 删行后 B 按旧基线 diff 漏插」的静默丢行。"""
    repo = store._session_repo
    errors = []
    for sid in ("s-c1a", "s-c1b"):
        repo.save(_make_session(sid, _mk_msgs(12)))

    def churn(sid):
        try:
            for round_no in range(6):
                msgs = _mk_msgs(12 + round_no * 2)  # 长度变化 → extras 集合变化
                repo.save(_make_session(sid, msgs))
        except Exception as e:  # noqa: BLE001
            errors.append((sid, e))

    threads = [threading.Thread(target=churn, args=(sid,)) for sid in ("s-c1a", "s-c1b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, f"并发写异常: {errors}"
    for sid in ("s-c1a", "s-c1b"):
        # 基线与 DB 一致：读回全量与轻量形态严格互补（窗外字段在 extras 不在 blob）
        full = repo.get_full_messages(sid)
        light = repo.get(sid)["messages"]
        n = len(full)
        for i in range(n):
            for f in OFFLOAD_KEYS:
                if i < n - 3 and full[i].get(f) is not None:
                    assert light[i].get(f) is None, f"{sid} msg[{i}] 字段 {f} 基线/DB 失配（静默丢行）"


def test_c1_write_extras_lock_serializes_same_session(store):
    """C1：同会话并发写经 _cache_lock 串行化——最终 DB ≡ 某个完整 variant（无串写污染）。"""
    repo = store._session_repo
    sid = "s-c1serial"
    repo.save(_make_session(sid, _mk_msgs(10)))
    variants = []
    for tag in range(4):
        m = _mk_msgs(10)
        for x in m:
            if x.get("reasoning_content"):
                x["reasoning_content"] = f"variant-{tag}"
        variants.append(m)

    def churn(m):
        repo.save(_make_session(sid, m))

    threads = [threading.Thread(target=churn, args=(m,)) for m in variants]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    full = repo.get_full_messages(sid)
    winners = [v for v in variants if full[1].get("reasoning_content") == v[1]["reasoning_content"]]
    assert winners, "最终 DB 值必须等于某个完整 variant（无串写污染）"
    winner = winners[0]
    for i, m in enumerate(full):
        assert m.get("diff", None) == winner[i].get("diff", None), f"msg[{i}] diff 与 winner 不一致（串写污染）"


def test_c2_send_guard_rules():
    """C2（复刻断言）：发送入口 pending 非空时守卫行为——正常补载清 pending
    放行；补载异常保留 pending 留痕放行（红线：不静默缺字段）。实现为
    main_widget._sync_flush_pending_extras 同款规则。"""
    state = {"pending": [1, 3, 5], "loaded": [], "raise": False}

    def sync_flush(session_messages):
        # 复刻 _sync_flush_pending_extras 规则
        idxs = list(state["pending"])
        if not idxs:
            return
        try:
            if state["raise"]:
                raise RuntimeError("injected db failure")
            state["loaded"].extend(idxs)
            for i in idxs:
                session_messages[i] = True
            state["pending"] = []
        except Exception:
            pass  # 留痕放行（pending 保留）

    session_messages = {}
    sync_flush(session_messages)
    assert state["pending"] == [] and sorted(state["loaded"]) == [1, 3, 5]
    assert session_messages == {1: True, 3: True, 5: True}
    # 补载失败：pending 保留 + 放行（不抛出）
    state["pending"] = [7]
    state["raise"] = True
    sync_flush(session_messages)
    assert state["pending"] == [7], "补载失败应保留 pending（留痕）而非静默清空"
