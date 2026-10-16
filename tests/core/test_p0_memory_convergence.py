# -*- coding: utf-8 -*-
"""批 IV 端到端收敛回归：messages 所有权收敛到 HistoryManager 单一入口。

4 个跨层不变量：
1. release_then_reload_then_continue_conversation
   释放 → 历史面板重开 → 消息完整 → 可继续对话（SQLite 懒加载回填链）。
2. reload_after_release_gets_full_messages_not_lightweight
   懒回填必须走 get_full_messages（主 blob + extras 合并）——轻量消息伪造
   空 arguments 会致工具完成框描述全空且重启不可逆（2026-09-12 根因）。
3. multi_window_close_releases_only_other_windows_sessions
   多窗口：某窗口关闭只释放非本窗口会话，本窗口活跃集合原样保留。
4. close_path_does_not_race_async_flush
   closeEvent 释放与 async flush 的竞态：Event 同步后台线程复现 flush
   序列，断言 save_session 收到非空 messages（pending 守卫生效）。
运行::
    python -m pytest tests/core/test_p0_memory_convergence.py -v
"""
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import app.utils.history_manager as history_module
from app.core.conversation.chat_session import ChatSession, SessionManager
from app.utils.history_manager import HistoryManager


def _make_manager(monkeypatch, tmp_path: Path) -> HistoryManager:
    monkeypatch.setattr(history_module, "get_app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(HistoryManager, "_init_storage", lambda self: None)
    manager = HistoryManager()
    manager._history_loaded = True
    return manager


def _messages(session_tag: str, n: int = 3) -> List[Dict[str, Any]]:
    return [{"role": "user", "content": f"{session_tag}-{i}"} for i in range(n)]


def test_release_then_reload_then_continue_conversation(monkeypatch, tmp_path):
    """释放 → 重开 → 消息完整 → 可继续对话（懒加载回填全链）。"""
    hm = _make_manager(monkeypatch, tmp_path)

    class _FakeStore:
        is_initialized = True

        def get_full_messages(self, session_id):
            return _messages("reloaded", 5)

    hm._use_sqlite = True
    hm._session_store = _FakeStore()
    hm._history_sessions = [
        {"session_id": "s-live", "title": "live", "messages": _messages("live")},
        {"session_id": "s-old", "title": "old", "messages": _messages("old")},
    ]

    # 新建会话 → 旧会话成非活跃 → 权威释放
    released = hm.release_messages_except({"s-live"})
    assert released == 1
    assert hm._history_sessions[1]["messages"] == []

    # 「历史面板重开」：SessionManager 加载该会话（懒回填链，必须走全量读取）
    session = ChatSession(name="old")
    session.session_id = "s-old"  # type: ignore[misc]
    session.set_messages(hm.get_session_messages("s-old") or [])
    assert len(session.messages) == 5, "重开必须拿到 SQLite 懒回填的完整消息"

    # 可继续对话：追加用户消息 + assistant 回复
    session.add_user_message(content="继续")
    session.add_assistant_message(content="好的", model_name="gpt-x")
    assert session.messages[-2]["role"] == "user"
    assert session.messages[-1]["role"] == "assistant"


def test_reload_after_release_gets_full_messages_not_lightweight(monkeypatch, tmp_path):
    """懒回填必须走 get_full_messages 合并 extras（防轻量消息伪造空 arguments）。

    历史根因（2026-09-12）：把轻量列表直接塞进内存会话 → 下次保存经
    normalize_message 给轻量消息伪造 arguments={} → extras 行被全删不插回
    → 工具参数数据永久丢失。
    """
    hm = _make_manager(monkeypatch, tmp_path)

    # Fake store：主 blob + extras 分离；轻量消息带 _x_idx 哨兵
    light_msg = {
        "role": "assistant",
        "content": "",
        "_x_idx": 1,
    }
    full_msg = {
        "role": "assistant",
        "content": "done",
        "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "edit", "arguments": '{"path":"a.py"}'}}],
    }

    class _FakeStore:
        is_initialized = True

        def get_full_messages(self, session_id):
            # 主 blob 轻量 + extras 合并 = 全量（模拟 repo.get_full_messages）
            assert session_id == "s-tools"
            return [dict(full_msg)]

    hm._use_sqlite = True
    hm._session_store = _FakeStore()

    got = hm.get_session_messages("s-tools")
    assert got == [dict(full_msg)], "必须走 get_full_messages 全量回填"
    assert "_x_idx" not in got[0], "轻量哨兵不得进入内存会话"
    assert got[0]["tool_calls"][0]["function"]["arguments"] == '{"path":"a.py"}', (
        "工具参数必须完整（轻量伪造空 arguments 的历史根因回归）"
    )


def test_multi_window_close_releases_only_other_windows_sessions(monkeypatch, tmp_path):
    """多窗口：窗口 B 关闭只释放非 B 会话；A 的活跃会话原样保留。"""
    hm = _make_manager(monkeypatch, tmp_path)
    hm._history_sessions = [
        {"session_id": "winA-1", "title": "A1", "messages": _messages("a1")},
        {"session_id": "winB-1", "title": "B1", "messages": _messages("b1")},
        {"session_id": "shared", "title": "S", "messages": _messages("s")},
    ]

    # 窗口 B 关闭：B 的 SessionManager 当前持有 winB-1（与 shared 无关）
    active_in_B = {"winB-1"}
    hm.release_messages_except(active_in_B)

    by_sid = {s["session_id"]: s for s in hm._history_sessions}
    assert by_sid["winB-1"]["messages"], "B 自己的会话保留（对象即将随 cleanup 回收，不提前置空）"
    assert by_sid["winA-1"]["messages"] == [], "A 的会话被释放（非 B 持有）"
    assert by_sid["shared"]["messages"] == []


def test_close_path_does_not_race_async_flush(monkeypatch, tmp_path):
    """closeEvent 释放 × async flush 竞态：Event 同步复现后台 flush 序列。

    真实时序：flush_async 主线程段清空 pending 并捕获 target 引用 → 启动
    后台线程 → 主线程执行 closeEvent 释放 → 后台 save_session(target)。
    此时 pending 已被清空，release_messages_except 的 pending 守卫读不到值
    ——真正的保护是「pending 会话必在本窗口活跃集合 own_ids 内被跳过」
    （pending 由本窗口 _auto_save 设置，其会话必被本窗口 SessionManager 持有）。
    断言：save_session 收到非空 messages（数据不被写空）。
    """
    hm = _make_manager(monkeypatch, tmp_path)
    hm._history_sessions = [
        {"session_id": "s-pending", "title": "t", "messages": _messages("p")},
        {"session_id": "s-other", "title": "o", "messages": _messages("o")},
    ]
    # 本窗口活跃集合：pending 会话（= 本窗口当前会话）在集合内
    own_ids = {"s-pending"}

    saved: List[Dict[str, Any]] = []
    bg_started = threading.Event()
    release_done = threading.Event()

    def fake_save_session(record):
        # 后台线程：等主线程释放动作完成后再落盘（拉大竞态窗口）
        release_done.wait(timeout=5)
        saved.append(record)

    hm.save_session = fake_save_session  # type: ignore[method-assign]

    def bg_flush():
        # 复现 flush_async 主线程段：捕获 target 引用并清 pending
        hm._pending_save_session_id = "s-pending"
        pending_id = hm._pending_save_session_id
        hm._pending_save_session_id = None
        target = next(s for s in hm._history_sessions if s.get("session_id") == pending_id)
        bg_started.set()
        # 此处主线程执行 closeEvent 释放
        release_done.wait(timeout=5)
        hm.save_session(target)

    t = threading.Thread(target=bg_flush)
    t.start()
    bg_started.wait(timeout=5)

    # 主线程：closeEvent 释放（own_ids 跳过本窗口会话；此时 pending 字段已被清空，
    # 入口 pending 守卫读不到值——本用例锁死 own_ids 跳过才是有效保护层）
    released = hm.release_messages_except(own_ids)
    release_done.set()
    t.join(timeout=5)

    assert released == 1, "仅非本窗口会话（s-other）被释放"
    by_sid = {s["session_id"]: s for s in hm._history_sessions}
    assert by_sid["s-pending"]["messages"], "flush target（本窗口会话）消息不得被置空"
    assert saved and saved[0].get("messages"), (
        f"后台 save_session 必须收到非空 messages，实际: {saved}"
    )
