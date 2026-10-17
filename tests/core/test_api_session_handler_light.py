# -*- coding: utf-8 -*-
"""[T20/N1] APIHistoryManager 会话轻量加载 + 懒回源 + 三 bug 修复回归。

R1: _load_from_sqlite 走 15 列投影（零全量 messages 解压，不碰 get_sessions）
R2: get_session_by_session_id 内存轻量命中 → 懒回源全量 + extras 合并 + system_prompt 哨兵回填
R3: get_session_by_index 轻量 → 按 session_id 委托回源
R4: save_session 补 last_time（新会话排序不垫底）
R5: archive_history 真删 SQLite 行（修复重载后复活）
R6: list_sessions 的 updated_at 取 last_time（字段名错位修复）

桩形态对齐 test_store_t2f.py：临时 SQLite SessionStore，秒级无 UI。
"""

from __future__ import annotations

import pytest

from app.gateway.local_service.session_handler import APIHistoryManager, APISessionHandler
from app.core.store.session_store import SessionStore
from app.utils.db_manager import DatabaseManager


@pytest.fixture()
def store(tmp_path):
    """临时 SQLite SessionStore（复用 t2f 单例复位范式）。"""
    SessionStore._instance = None
    DatabaseManager._instance = None
    st = SessionStore(str(tmp_path / "db"))
    assert st.is_initialized, "SessionStore 应初始化成功"
    yield st
    try:
        st.close()
    except Exception:  # noqa: BLE001
        pass
    SessionStore._instance = None
    DatabaseManager._instance = None


def _make_manager(monkeypatch, store) -> APIHistoryManager:
    """绕过 _init_sqlite（不走 backend 门面），直挂临时 store。"""
    monkeypatch.setattr(APIHistoryManager, "_init_sqlite", lambda self: None)
    mgr = APIHistoryManager(None)
    mgr._session_store = store
    mgr._api_sessions = []
    mgr._load_from_sqlite()
    return mgr


def _save_full(store: SessionStore, sid: str, system_prompt: str = ""):
    """经 store 落一个含 extras 字段的全量会话（save 链自会剥离进 extras）。"""
    msgs = [
        {
            "role": "assistant",
            "content": "done",
            "arguments": {"command": "ls -la"},
            "diff": "--- a\n+++ b\n",
            "reasoning_content": "思考中",
        },
        {"role": "user", "content": "run ls"},
    ]
    assert store.save_session(
        {
            "session_id": sid,
            "title": f"会话 {sid}",
            "messages": msgs,
            "message_count": len(msgs),
            "system_prompt": system_prompt,
            "last_time": "2026-10-10 10:00:00",
            "created_at": "2026-10-10 10:00:00",
            "updated_at": "2026-10-10 10:00:00",
        }
    )


# ── R1：轻量投影 ──


def test_r1_load_uses_lightweight_projection(store, monkeypatch):
    """_load_from_sqlite 必须走 get_sessions_lightweight：记录无 messages，且不碰全量 get_sessions。"""
    _save_full(store, "r1-a")
    _save_full(store, "r1-b")

    calls = {"full": 0, "light": 0}
    real_full, real_light = store.get_sessions, store.get_sessions_lightweight
    monkeypatch.setattr(store, "get_sessions", lambda *a, **k: (calls.__setitem__("full", calls["full"] + 1), real_full(*a, **k))[1])
    monkeypatch.setattr(
        store, "get_sessions_lightweight", lambda *a, **k: (calls.__setitem__("light", calls["light"] + 1), real_light(*a, **k))[1]
    )

    mgr = _make_manager(monkeypatch, store)

    assert calls["full"] == 0, "轻量加载不得触发全量 BLOB 解压"
    assert calls["light"] == 1
    assert len(mgr._api_sessions) == 2
    assert all("messages" not in s or not s.get("messages") for s in mgr._api_sessions), "轻量记录不得携带 messages"


# ── R2：懒回源 + extras 合并 + 哨兵回填 ──


def test_r2_lazy_fetch_merges_extras_and_backfills_system_prompt(store, monkeypatch):
    """内存轻量命中 → get_session 回源全量 → extras 合并恢复剥离字段 + system_prompt 回填。"""
    _save_full(store, "r2-x", system_prompt="你是助手")
    mgr = _make_manager(monkeypatch, store)
    # 模拟轻量内存态：messages 空 + system_prompt None 哨兵
    light = dict(mgr._api_sessions[0])
    light["messages"] = []
    light["system_prompt"] = None
    mgr._api_sessions = [light]

    s = mgr.get_session_by_session_id("r2-x")
    assert s is not None
    msgs = s["messages"]
    assert msgs, "懒回源后 messages 应非空"
    assert msgs[0].get("arguments") == {"command": "ls -la"}, "extras 合并应恢复 arguments"
    assert msgs[0].get("reasoning_content") == "思考中", "extras 合并应恢复 reasoning_content"
    assert s["system_prompt"] is not None, "None 哨兵应借全量查询回填"


# ── R3：index 委托 ──


def test_r3_index_delegate_returns_full_messages(store, monkeypatch):
    """get_session_by_index 轻量命中 → 按 session_id 委托 → 返回全量 messages。"""
    _save_full(store, "r3-x")
    mgr = _make_manager(monkeypatch, store)
    assert all(not s.get("messages") for s in mgr._api_sessions)

    msgs = mgr.get_session_by_index(0)
    assert isinstance(msgs, list) and msgs, "轻量 index 应委托回源拿到非空 messages"
    assert any(m.get("role") == "assistant" for m in msgs)


# ── R4：save 补 last_time ──


def test_r4_save_sets_last_time_and_new_session_ranks_first(store, monkeypatch):
    """save_session 落 last_time：新会话在 get_history_list 排序中不垫底。"""
    mgr = _make_manager(monkeypatch, store)

    mgr.save_session([{"role": "user", "content": "旧"}], "旧会话", "r4-old")
    mgr.save_session([{"role": "user", "content": "新"}], "新会话", "r4-new")

    titles = [s.get("title") for s in mgr.get_history_list()]
    assert titles[0] == "新会话", f"新会话应排最前（last_time 修复），实际: {titles}"
    assert all(s.get("last_time") for s in mgr._api_sessions), "所有内存记录应带 last_time"


# ── R5：归档真删 ──


def test_r5_archive_deletes_store_row_no_revival(store, monkeypatch):
    """archive_history 后 SQLite 行必须真删：重载不复活。"""
    _save_full(store, "r5-x")
    mgr = _make_manager(monkeypatch, store)
    idx = mgr.find_index_by_session_id("r5-x")
    assert idx >= 0

    assert mgr.archive_history(idx) is True
    assert store.get_session("r5-x") is None, "归档必须删除 SQLite 行"

    # 重载（模拟重启）：会话不得复活
    mgr2 = _make_manager(monkeypatch, store)
    assert mgr2.get_session_by_session_id("r5-x") is None, "重载后归档会话不得复活"


# ── R6：list_sessions 字段 ──


def test_r6_list_sessions_reads_last_time(store, monkeypatch):
    """APISessionHandler.list_sessions 的 updated_at 取 last_time（修复字段名错位空串）。"""
    _save_full(store, "r6-x")
    mgr = _make_manager(monkeypatch, store)

    handler = APISessionHandler(lambda: None)
    w = object()
    monkeypatch.setattr(handler, "_current_widget", lambda: w)
    handler._api_history_manager = mgr
    handler._api_history_manager_widget_id = id(w)

    lst = handler.list_sessions()
    assert lst, "列表不应为空"
    assert lst[0]["id"] == "r6-x"
    # 链路口径：updated_at 取 last_time（store 会刷新时间戳，故不锁死值，
    # 只锁「等于记录 last_time 且非空」——原 bug 取 last_updated 键恒空串）
    raw_last = mgr.get_history_list()[0].get("last_time")
    assert raw_last, "内存记录应带 last_time"
    assert lst[0]["updated_at"] == raw_last, "updated_at 应取 last_time（原 last_updated 键位错取空串）"
