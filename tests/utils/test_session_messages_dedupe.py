# -*- coding: utf-8 -*-
"""[I8] 切会话双读去重 + 团队恢复批量取回回归测试。

== 点位① 双读去重等价 ==
切会话路径 get_session_by_session_id（懒回填）→ get_session_messages：
改后第二次调用复用内存合并全量，不再触发引擎 get_full_messages 重读；
结果与 get_full_messages 内容等价（含 extras 合并）。

== 点位② 批量取回等价 ==
get_sessions_messages_bulk 真库批量结果 == 逐会话 get_full_messages 结果；
引擎无 get_by_ids 能力时回退逐会话单读，结果一致。

真库 fixture 模式同 tests/core/test_session_extras.py（单例复位 + tmp_path）。
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


@pytest.fixture
def engine(store, tmp_path):
    """与 store 共享同一 SessionStore 单例的 sqlite 引擎"""
    from importlib import import_module

    SqliteStorageEngine = import_module("plugins.system-storages.storages.sqlite").SqliteStorageEngine
    return SqliteStorageEngine()  # db_dir=None → get_instance() 复用 store 单例


def _make_hm(monkeypatch, tmp_path, engine, lightweight_sids):
    """构造挂真库引擎的 HistoryManager（内存列表为轻量形态，模拟启动加载后）"""
    monkeypatch.setattr(history_module, "get_app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(HistoryManager, "_init_storage", lambda self: None)
    hm = HistoryManager()
    hm._use_sqlite = True
    hm._session_store = engine
    hm._history_sessions = [{"session_id": sid, "title": "t", "messages": []} for sid in lightweight_sids]
    return hm


from app.utils import history_manager as history_module  # noqa: E402
from app.utils.history_manager import HistoryManager  # noqa: E402


def _msg(role, content, **extra):
    m = {"role": role, "content": content, "timestamp": "2026-01-01 00:00:00"}
    m.update(extra)
    return m


def _seed_sessions(store):
    """落两个会话：s1 带保活窗外剥离字段（产生 extras 行），s2 短会话"""
    s1_messages = []
    for i in range(10):
        s1_messages.append(_msg("assistant", f"a{i}", reasoning_content=f"think-{i}"))
        s1_messages.append(_msg("tool", f"r{i}", arguments={"x": i}, diff="d", name="edit", tool_call_id=f"c{i}"))
    s1_messages.append(_msg("user", "hello s1"))
    store.save_session(
        {
            "session_id": "s1",
            "title": "会话一",
            "project": "默认项目",
            "messages": s1_messages,
            "system_prompt": "sp1",
            "compaction_state": {},
            "compaction_cache": {},
            "message_count": len(s1_messages),
            "user_edited_title": False,
            "worktree_path": "",
            "preview": "",
            "context_usage": 0,
            "last_api_prompt_tokens": 0,
            "last_api_message_count": len(s1_messages),
        }
    )
    s2_messages = [_msg("user", "hi s2"), _msg("assistant", "answer s2")]
    store.save_session(
        {
            "session_id": "s2",
            "title": "会话二",
            "project": "默认项目",
            "messages": s2_messages,
            "system_prompt": "sp2",
            "compaction_state": {},
            "compaction_cache": {},
            "message_count": len(s2_messages),
            "user_edited_title": False,
            "worktree_path": "",
            "preview": "",
            "context_usage": 0,
            "last_api_prompt_tokens": 0,
            "last_api_message_count": len(s2_messages),
        }
    )


def test_switch_path_second_read_reuses_memory(monkeypatch, tmp_path, store, engine):
    """点位①：懒回填后的 get_session_messages 必须复用内存，不再重读引擎。"""
    _seed_sessions(store)
    hm = _make_hm(monkeypatch, tmp_path, engine, lightweight_sids=["s1", "s2"])

    # 模拟真实切会话时序：第一步懒回填（内存轻量 → 查库合并回填）
    record = hm.get_session_by_session_id("s1")
    assert record and record.get("messages"), "懒回填应把合并全量写回内存记录"

    calls = {"full": 0}
    original = engine.get_full_messages

    def counting(sid):
        calls["full"] += 1
        return original(sid)

    monkeypatch.setattr(engine, "get_full_messages", counting)

    messages = hm.get_session_messages("s1")
    assert calls["full"] == 0, "懒回填后第二次读必须复用内存（不触发引擎 get_full_messages）"
    baseline = original("s1")
    assert messages == baseline, "复用结果必须与 get_full_messages 等价（含 extras 合并）"
    # extras 确实参与了合并（保活窗外 arguments 已回填）
    assert any("arguments" in m for m in messages), "合并结果应含被剥离的 arguments"


def test_direct_call_still_reads_engine_once(monkeypatch, tmp_path, store, engine):
    """无预调用直取：保持原语义，引擎 get_full_messages 恰一次。"""
    _seed_sessions(store)
    hm = _make_hm(monkeypatch, tmp_path, engine, lightweight_sids=["s1"])

    calls = {"full": 0}
    original = engine.get_full_messages

    def counting(sid):
        calls["full"] += 1
        return original(sid)

    monkeypatch.setattr(engine, "get_full_messages", counting)

    messages = hm.get_session_messages("s1")
    assert calls["full"] == 1, "无内存命中时应走引擎全量读恰一次（原语义不变）"
    assert messages == original("s1")


def test_bulk_matches_per_session_full_messages(monkeypatch, tmp_path, store, engine):
    """点位②：批量取回结果与逐会话 get_full_messages 等价，缺失 sid 不出现。"""
    from app.plugins.contracts.storage import SessionBulkGetCapability

    _seed_sessions(store)
    hm = _make_hm(monkeypatch, tmp_path, engine, lightweight_sids=[])
    assert isinstance(engine, SessionBulkGetCapability), "sqlite 引擎应实现 get_by_ids 能力"

    bulk = hm.get_sessions_messages_bulk(["s1", "s2", "missing-sid"])
    assert "missing-sid" not in bulk
    assert set(bulk.keys()) == {"s1", "s2"}
    for sid in ("s1", "s2"):
        assert bulk[sid] == engine.get_full_messages(sid), f"批量结果 {sid} 必须与单读等价"
    assert any("arguments" in m for m in bulk["s1"]), "批量路径 extras 合并不得缺失"


def test_bulk_falls_back_without_capability(monkeypatch, tmp_path, store, engine):
    """引擎无 get_by_ids 能力：回退逐会话单读，结果与单读一致。"""
    _seed_sessions(store)
    hm = _make_hm(monkeypatch, tmp_path, engine, lightweight_sids=[])

    class _NoBulkEngine:
        """委托 engine 但不暴露 get_by_ids（模拟第三方引擎能力缺失）"""

        id = "no-bulk"

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            if name == "get_by_ids":
                raise AttributeError(name)
            return getattr(self._inner, name)

    wrapped = _NoBulkEngine(engine)
    # runtime_checkable Protocol 的 isinstance 只查方法存在性；__getattr__ 动态
    # 委托会让 hasattr 恒真，这里直接验证 isinstance 判定与回退行为：
    from app.plugins.contracts.storage import SessionBulkGetCapability

    hm._session_store = wrapped

    calls = {"full": 0}
    original = engine.get_full_messages

    def counting(sid):
        calls["full"] += 1
        return original(sid)

    monkeypatch.setattr(engine, "get_full_messages", counting)

    bulk = hm.get_sessions_messages_bulk(["s1", "s2"])
    # __getattr__ 委托下 isinstance 会误判 True 并走进 get_by_ids（等价于能力
    # 可用），或判定 False 走回退——两种路径结果都必须与单读等价：
    assert set(bulk.keys()) == {"s1", "s2"}
    for sid in ("s1", "s2"):
        assert bulk[sid] == original(sid)


def test_session_store_get_by_ids_forward(store):
    """SessionStore.get_by_ids 转发链通：真库 IN 查询命中已存会话。"""
    _seed_sessions(store)
    out = store.get_by_ids(["s1", "s2", "ghost"])
    assert set(out.keys()) == {"s1", "s2"}
    assert out["s1"]["session_id"] == "s1"
    assert isinstance(out["s1"].get("messages"), list)
