# -*- coding: utf-8 -*-
"""I6 回归：引用会话 N+1 串行读批量化。

覆盖：
① SessionRepository.get_by_ids：IN 批量查询 + 反序列化 + 缓存失效 + 分批防
   SQLite 变量数上限；
② build_referenced_context 批量命中路径（探测 SessionBulkGetCapability 成功，
   一次批量调用替代 N 次逐个 get）；
③ 降级路径（引擎无批量能力 / 批量抛异常 → 原逐个读；批量 miss 单查兜底）。
"""

from unittest.mock import MagicMock

from app.core.store import serde


def _make_repo():
    """构造最小 SessionRepository（同 test_session_repository_cache_lock 模式）。"""
    from app.core.store.session_repository import SessionRepository

    db = MagicMock()
    db.is_connected = True
    repo = SessionRepository(db)
    return repo, db


def _row(sid: str, user: str = "问题", assistant: str = "回答") -> dict:
    """构造一行库记录（messages 为序列化 blob，同 get() 的输入形态）。"""
    return {
        "session_id": sid,
        "title": f"T-{sid}",
        "project": "P",
        "messages": serde.serialize(
            [
                {"role": "user", "content": user},
                {"role": "assistant", "content": assistant},
            ]
        ),
        "system_prompt": "",
        "compaction_state": serde.serialize({}),
        "compaction_cache": serde.serialize({}),
        "message_count": 2,
        "user_edited_title": 0,
        "worktree_path": "",
        "preview": "",
        "context_usage": 0,
        "last_api_prompt_tokens": 0,
        "last_api_message_count": 0,
        "created_at": "2026-01-01 00:00:00",
        "updated_at": "2026-01-01 00:00:00",
    }


# ── ① get_by_ids 本体 ───────────────────────────────────────────

def test_get_by_ids_single_in_query_and_cache_invalidation():
    """批量：一次 IN 查询、结果按 sid 建索引、成功取回的 sid 失效内容缓存。"""
    repo, db = _make_repo()
    db.execute_sql.return_value = (True, [_row("a"), _row("b")])
    repo._content_hash_cache["a"] = (1, 2)
    repo._content_hash_cache["b"] = (3, 4)
    repo._content_hash_cache["c"] = (5, 6)  # 未取回的 sid 不动

    out = repo.get_by_ids(["a", "b"])
    assert set(out) == {"a", "b"}
    assert out["a"]["session_id"] == "a"
    assert out["b"]["messages"][1]["content"] == "回答"
    # 与 get() 语义对齐：外部加载最新数据 → 失效缓存
    assert "a" not in repo._content_hash_cache and "b" not in repo._content_hash_cache
    assert "c" in repo._content_hash_cache
    # 一次 IN 查询（非 N 次 get）
    sql_calls = [c.args[0] for c in db.execute_sql.call_args_list]
    assert sum(1 for s in sql_calls if "IN (" in s) == 1
    assert any("WHERE session_id IN (?,?)" in s for s in sql_calls)


def test_get_by_ids_empty_uninitialized_and_sql_failure():
    """空 ids / 未初始化 / SQL 失败 → {}（调用方降级逐个读）。"""
    repo, db = _make_repo()
    assert repo.get_by_ids([]) == {}
    db.is_connected = False  # is_initialized 为 property（随 db 连接态）
    assert repo.get_by_ids(["a"]) == {}
    db.is_connected = True
    db.execute_sql.return_value = (False, "disk error")
    assert repo.get_by_ids(["a"]) == {}


def test_get_by_ids_bad_row_skipped_and_batched_over_500():
    """单行反序列化失败跳过不拖垮整批；>500 分批（SQLite 变量数上限防御）。"""
    repo, db = _make_repo()
    ids = [f"s{i}" for i in range(1200)]
    db.execute_sql.side_effect = [
        (True, [_row("s0")]),
        (True, []),  # 第二批空
        (True, [_row("s1000")]),
    ]
    # 让第一行反序列化抛异常：坏行跳过、好行保留
    calls = {"n": 0}
    orig = repo._row_to_session

    def flaky(row):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TypeError("bad blob")
        return orig(row)

    repo._row_to_session = flaky
    try:
        out = repo.get_by_ids(ids)
    finally:
        repo._row_to_session = orig
    assert out.get("s1000") is not None and out.get("s0") is None
    assert db.execute_sql.call_count == 3  # 1200 ids → 500/500/200 三批


# ── ②③ build_referenced_context 批量命中 / 降级 ─────────────────

class _FakeStorage:
    """无批量能力的存储引擎（探测 False → 降级逐个读）。"""

    id = "fake"

    def __init__(self, sessions: dict):
        self._sessions = sessions
        self.get_calls: list = []

    def get(self, session_id):
        self.get_calls.append(session_id)
        return self._sessions.get(session_id)


class _BulkStorage(_FakeStorage):
    """带批量能力的存储引擎（isinstance(SessionBulkGetCapability) 命中）。"""

    id = "fake-bulk"

    def __init__(self, sessions: dict, bulk_by_ids=None, raise_in_bulk: bool = False):
        super().__init__(sessions)
        self.bulk_calls: list = []
        self._bulk_by_ids = bulk_by_ids
        self._raise_in_bulk = raise_in_bulk

    def get_by_ids(self, session_ids):
        self.bulk_calls.append(list(session_ids))
        if self._raise_in_bulk:
            raise RuntimeError("bulk boom")
        return dict(self._bulk_by_ids or {})


def _patch_storage(monkeypatch, storage):
    from app.core.conversation import backend as backend_mod

    monkeypatch.setattr(backend_mod, "get_session_storage", lambda: storage)


def test_bulk_hit_replaces_n_serial_gets(monkeypatch):
    """批量命中：一次 get_by_ids、零逐个 get、浓缩输出与逐个读等价。"""
    sessions = {
        "a": {"session_id": "a", "title": "甲", "messages": [
            {"role": "user", "content": "问一"},
            {"role": "assistant", "content": "答一"},
        ]},
        "b": {"session_id": "b", "title": "乙", "messages": [
            {"role": "user", "content": "问二"},
        ]},
    }
    storage = _BulkStorage(sessions, bulk_by_ids=sessions)
    _patch_storage(monkeypatch, storage)

    from app.core.context.session_reference import build_referenced_context

    out = build_referenced_context(["a", "b"])
    assert storage.bulk_calls == [["a", "b"]]
    assert storage.get_calls == [], "批量命中后不得再逐个 get"
    assert "历史会话「甲」" in out and "用户: 问一" in out and "助手: 答一" in out
    assert "历史会话「乙」" in out and "用户: 问二" in out
    assert out.startswith("<referenced_sessions>") and out.endswith("</referenced_sessions>")


def test_bulk_miss_falls_back_to_single_get(monkeypatch):
    """批量部分 miss（库中已删的 id）→ 该 id 单查兜底，语义与逐个读一致。"""
    sessions = {
        "a": {"session_id": "a", "title": "甲", "messages": [{"role": "user", "content": "问一"}]},
        "b": {"session_id": "b", "title": "乙", "messages": [{"role": "user", "content": "问二"}]},
    }
    storage = _BulkStorage(sessions, bulk_by_ids={"a": sessions["a"]})  # 批量只回了 a
    _patch_storage(monkeypatch, storage)

    from app.core.context.session_reference import build_referenced_context

    out = build_referenced_context(["a", "b"])
    assert storage.get_calls == ["b"], "仅批量 miss 的 id 单查兜底"
    assert "历史会话「甲」" in out and "历史会话「乙」" in out


def test_no_bulk_capability_falls_back_to_legacy_loop(monkeypatch):
    """降级路径：引擎无 get_by_ids（探测 False）→ 原逐个读，行为零变化。"""
    sessions = {
        "x": {"session_id": "x", "title": "丙", "messages": [{"role": "user", "content": "问三"}]},
    }
    storage = _FakeStorage(sessions)
    _patch_storage(monkeypatch, storage)

    from app.core.context.session_reference import build_referenced_context

    out = build_referenced_context(["x"])
    assert storage.get_calls == ["x"]
    assert "历史会话「丙」" in out and "用户: 问三" in out


def test_bulk_exception_falls_back_to_legacy_loop(monkeypatch):
    """降级路径：get_by_ids 抛异常 → 整体降级逐个读，不阻塞发送。"""
    sessions = {
        "y": {"session_id": "y", "title": "丁", "messages": [{"role": "user", "content": "问四"}]},
    }
    storage = _BulkStorage(sessions, raise_in_bulk=True)
    _patch_storage(monkeypatch, storage)

    from app.core.context.session_reference import build_referenced_context

    out = build_referenced_context(["y"])
    assert storage.bulk_calls == [["y"]]
    assert storage.get_calls == ["y"], "批量异常后逐个读兜底"
    assert "用户: 问四" in out
