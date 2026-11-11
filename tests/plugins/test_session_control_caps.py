# -*- coding: utf-8 -*-
"""会话控制能力（session.interrupt / new / send）：入队语义与窗口精确匹配"""

from unittest.mock import MagicMock

import queue as _queue

from app.core.conversation.action_queue import get_action_queue
from app.plugins.capabilities.providers.ui_core import _session_control


def _ui_window(wid: str):
    w = MagicMock()
    w._window_id = wid
    return w


def _busy_window(wid: str, busy: bool):
    """带 backend 的窗口 stub：chat_engine.is_streaming 控制忙闲，hook 队列用真 Queue"""
    engine = MagicMock()
    engine.is_streaming = busy
    backend = MagicMock()
    backend.chat_engine = engine
    backend._hook_message_queue = _queue.Queue()
    w = MagicMock()
    w._window_id = wid
    w.backend = backend
    return w, backend


def test_ui_provider_enqueues_in_order():
    q = get_action_queue()
    q.clear("w-test")
    api = _session_control(_ui_window("w-test"))
    assert api["interrupt"]() is True
    assert api["new"]() is True
    assert api["send"]("继续", ephemeral=True) is True
    assert [a["action"] for a in q.peek("w-test")] == ["interrupt", "new", "send"]
    q.clear("w-test")


def test_send_payload_keeps_ephemeral_flag():
    q = get_action_queue()
    q.clear("w-test2")
    api = _session_control(_ui_window("w-test2"))
    api["send"]("补写", ephemeral=False)
    payload = q.peek("w-test2")[0]["payload"]
    assert payload == {"text": "补写", "ephemeral": False}
    q.clear("w-test2")


def test_ui_provider_rejects_without_window():
    api = _session_control(None)
    assert api["interrupt"]() is False


def test_dedup_interrupt_within_window():
    q = get_action_queue()
    q.clear("w-test3")
    api = _session_control(_ui_window("w-test3"))
    assert api["interrupt"]() is True
    assert api["interrupt"]() is False
    q.clear("w-test3")


# ──────────────── send 三模式（queue/skip/hook） ────────────────


def test_hook_mode_busy_injects_to_hook_queue():
    """hook：忙时消息进 _hook_message_queue（不建动作、不打断）"""
    q = get_action_queue()
    q.clear("w-hook-busy")
    win, backend = _busy_window("w-hook-busy", busy=True)
    api = _session_control(win)
    assert api["send"]("催收提醒", mode="hook") is True
    msgs = []
    while True:
        try:
            msgs.append(backend._hook_message_queue.get_nowait())
        except _queue.Empty:
            break
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert msgs[0]["_hook_event"] == "SessionSend"
    assert "催收提醒" in msgs[0]["content"]
    assert q.peek("w-hook-busy") == [], "hook 注入不得同时入动作队列"


def test_hook_mode_custom_hook_event():
    """hook：hook_event 自定义事件标记透传（子智能体结果注入用 SubAgentFinished）"""
    q = get_action_queue()
    q.clear("w-hook-event")
    win, backend = _busy_window("w-hook-event", busy=True)
    api = _session_control(win)
    assert api["send"]("子智能体结果", mode="hook", hook_event="SubAgentFinished") is True
    msgs = []
    while True:
        try:
            msgs.append(backend._hook_message_queue.get_nowait())
        except _queue.Empty:
            break
    assert len(msgs) == 1
    assert msgs[0]["_hook_event"] == "SubAgentFinished"
    assert "sub-agent-finished-hook" in msgs[0]["content"]


def test_hook_mode_idle_falls_back_to_enqueue():
    """hook：空闲时降级普通 send（开新轮，比滞留 hook 队列到下轮开头更及时）"""
    q = get_action_queue()
    q.clear("w-hook-idle")
    win, backend = _busy_window("w-hook-idle", busy=False)
    api = _session_control(win)
    assert api["send"]("到点提醒", mode="hook") is True
    assert backend._hook_message_queue.empty()
    assert [a["action"] for a in q.peek("w-hook-idle")] == ["send"]
    q.clear("w-hook-idle")


def test_skip_mode_busy_rejects():
    """skip：忙时拒绝（返回 False），两队列都干净"""
    q = get_action_queue()
    q.clear("w-skip-busy")
    win, backend = _busy_window("w-skip-busy", busy=True)
    api = _session_control(win)
    assert api["send"]("可丢通知", mode="skip") is False
    assert backend._hook_message_queue.empty()
    assert q.peek("w-skip-busy") == []


def test_skip_mode_idle_enqueues():
    q = get_action_queue()
    q.clear("w-skip-idle")
    win, _backend = _busy_window("w-skip-idle", busy=False)
    api = _session_control(win)
    assert api["send"]("空闲期通知", mode="skip") is True
    assert [a["action"] for a in q.peek("w-skip-idle")] == ["send"]
    q.clear("w-skip-idle")


def test_invalid_mode_falls_back_to_queue():
    """未知 mode 回落 queue（默认语义），busy 下也照常入队"""
    q = get_action_queue()
    q.clear("w-bad-mode")
    win, _backend = _busy_window("w-bad-mode", busy=True)
    api = _session_control(win)
    assert api["send"]("普通排队", mode="bogus") is True
    assert [a["action"] for a in q.peek("w-bad-mode")] == ["send"]
    q.clear("w-bad-mode")


def test_missing_backend_safe_fallback():
    """backend 拿不到（None）：hook/skip 均安全回落普通入队"""
    q = get_action_queue()
    q.clear("w-no-backend")
    w = MagicMock()
    w._window_id = "w-no-backend"
    w.backend = None
    api = _session_control(w)
    assert api["send"]("x", mode="hook") is True
    assert api["send"]("y", mode="skip") is True
    assert [a["action"] for a in q.peek("w-no-backend")] == ["send", "send"]
    q.clear("w-no-backend")
