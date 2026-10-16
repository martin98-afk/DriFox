# -*- coding: utf-8 -*-
"""HistoryManager.release_messages_except 权威释放入口回归测试（批 IV）。

覆盖 9 个不变量：
1. skips_active          活跃集合内的会话不释放
2. releases_others       非活跃会话置空 + 打 _messages_released 标记
3. skips_pending         pending 会话跳过（核心回归：防 _do_save 用空列表覆盖 DB）
4. skips_already_empty   已空 messages 的会话不重复计数
5. empty_set_releases_all 活跃集合为空 → 全部释放
6. returns_count         返回值 = 本次实际释放数
7. then_do_save_safe     释放后 _do_save 序列不回写空列表（守卫语义）
8. then_flush_async_safe 释放后 flush_async 目标捕获链安全
9. does_not_touch_explicit_clear 无标记的空 messages 会话不被误打标
运行::
    python -m pytest tests/utils/test_history_manager_release.py -v
"""
from pathlib import Path

import app.utils.history_manager as history_module
from app.utils.history_manager import HistoryManager


def _make_manager(monkeypatch, tmp_path: Path) -> HistoryManager:
    monkeypatch.setattr(history_module, "get_app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(HistoryManager, "_init_storage", lambda self: None)
    manager = HistoryManager()
    manager._history_loaded = True  # 跳过 SQLite 懒加载
    return manager


def _session(sid: str, messages: list | None = None) -> dict:
    return {
        "session_id": sid,
        "title": f"会话 {sid}",
        "messages": messages if messages is not None else [{"role": "user", "content": f"hi-{sid}"}],
    }


def test_skips_active(monkeypatch, tmp_path):
    """活跃集合内的会话不被释放（其余正常释放）。"""
    hm = _make_manager(monkeypatch, tmp_path)
    hm._history_sessions = [_session("a"), _session("b")]
    released = hm.release_messages_except({"a"})
    assert released == 1  # b 被释放
    assert hm._history_sessions[0]["messages"], "活跃会话消息必须保留"
    assert hm._history_sessions[0].get("_messages_released") is None


def test_releases_others(monkeypatch, tmp_path):
    """非活跃会话置空 + 打 _messages_released 标记。"""
    hm = _make_manager(monkeypatch, tmp_path)
    hm._history_sessions = [_session("a"), _session("b"), _session("c")]
    released = hm.release_messages_except({"a"})
    assert released == 2
    assert hm._history_sessions[1]["messages"] == []
    assert hm._history_sessions[1]["_messages_released"] is True
    assert hm._history_sessions[2]["_messages_released"] is True
    # 元数据保留（title 不动）
    assert hm._history_sessions[1]["title"] == "会话 b"


def test_skips_pending(monkeypatch, tmp_path):
    """核心回归：pending 会话跳过释放（防 _do_save 用 serialize([]) 覆盖 DB）。"""
    hm = _make_manager(monkeypatch, tmp_path)
    hm._history_sessions = [_session("a"), _session("pending-1")]
    hm._pending_save_session_id = "pending-1"
    released = hm.release_messages_except(set())
    assert released == 1
    assert hm._history_sessions[1]["messages"], "pending 会话消息必须保留（竞态守卫）"
    assert "_messages_released" not in hm._history_sessions[1]


def test_skips_already_empty(monkeypatch, tmp_path):
    """已空 messages 的会话不重复计数、不重复打标。"""
    hm = _make_manager(monkeypatch, tmp_path)
    hm._history_sessions = [_session("a"), _session("empty", messages=[])]
    released = hm.release_messages_except(set())
    assert released == 1  # 只计 a
    assert "_messages_released" not in hm._history_sessions[1]


def test_empty_set_releases_all(monkeypatch, tmp_path):
    """活跃集合为空 → 全部非 pending 会话释放。"""
    hm = _make_manager(monkeypatch, tmp_path)
    hm._history_sessions = [_session("a"), _session("b"), _session("c")]
    released = hm.release_messages_except(set())
    assert released == 3
    assert all(s["messages"] == [] for s in hm._history_sessions)


def test_returns_count(monkeypatch, tmp_path):
    """返回值 = 本次实际释放数（幂等重跑为 0）。"""
    hm = _make_manager(monkeypatch, tmp_path)
    hm._history_sessions = [_session("a"), _session("b")]
    assert hm.release_messages_except({"a"}) == 1
    assert hm.release_messages_except({"a"}) == 0  # 第二次已无可释放


def test_then_do_save_safe(monkeypatch, tmp_path):
    """释放后走 _do_save 序列：释放态（有标记）空列表不得覆盖 DB。"""
    hm = _make_manager(monkeypatch, tmp_path)
    hm._history_sessions = [_session("keep"), _session("released")]
    hm.release_messages_except({"keep"})

    saved = []

    def fake_save_session(record):
        saved.append(record)
        return True

    hm.save_session = fake_save_session  # type: ignore[method-assign]
    # 模拟 _do_save 的空消息守卫口径（与 history_manager._do_save 同判据）：
    # 有 _messages_released 标记的空 messages → 跳过落库
    for s in hm._history_sessions:
        if not s.get("messages") and s.get("_messages_released"):
            continue
        hm.save_session(s)
    assert [r["session_id"] for r in saved] == ["keep"], "释放态会话不得回写 DB"


def test_then_flush_async_safe(monkeypatch, tmp_path):
    """释放后 flush_async 目标捕获链：pending 会话先于释放被保护。"""
    hm = _make_manager(monkeypatch, tmp_path)
    hm._history_sessions = [_session("flush-target"), _session("other")]
    hm._pending_save_session_id = "flush-target"
    hm.release_messages_except({"other"})

    # flush_async 主线程段：读走 pending 并捕获 target（引用而非拷贝）
    pending_id = hm._pending_save_session_id
    hm._pending_save_session_id = None
    target = next(s for s in hm._history_sessions if s.get("session_id") == pending_id)
    # 释放发生在 flush 之后也不会误伤（target 有 pending 守卫未被置空）
    assert target["messages"], "flush 目标必须保有完整消息（pending 守卫在释放前生效）"


def test_does_not_touch_explicit_clear(monkeypatch, tmp_path):
    """无标记的空 messages 会话（显式清空）不被误打释放标。"""
    hm = _make_manager(monkeypatch, tmp_path)
    cleared = _session("cleared", messages=[])
    hm._history_sessions = [cleared]
    hm.release_messages_except(set())
    assert "_messages_released" not in cleared, (
        "显式清空（/compact --clear）语义必须保留：不打标记，DB 照写空"
    )
