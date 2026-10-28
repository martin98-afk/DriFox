# -*- coding: utf-8 -*-
"""窗口级动作队列：入队顺序、去重、失败中止、窗口隔离"""

from app.core.conversation.action_queue import WindowActionQueue


def test_enqueue_and_drain_in_order():
    q = WindowActionQueue()
    calls = []
    q.enqueue("w1", "interrupt", {})
    q.enqueue("w1", "new", {})
    q.enqueue("w1", "send", {"text": "继续", "ephemeral": True})
    handlers = {
        "interrupt": lambda p: calls.append("i"),
        "new": lambda p: calls.append("n"),
        "send": lambda p: calls.append(("s", p["text"])),
    }
    assert q.drain("w1", handlers=handlers) == 3
    assert calls == ["i", "n", ("s", "继续")]
    assert q.drain("w1", handlers={}) == 0  # 已清空


def test_dedup_interrupt_and_new():
    q = WindowActionQueue()
    assert q.enqueue("w1", "interrupt", {}) is True
    assert q.enqueue("w1", "interrupt", {}) is False  # 去重
    assert q.enqueue("w1", "new", {}) is True
    assert q.enqueue("w1", "new", {}) is False
    assert len(q.peek("w1")) == 2


def test_failure_aborts_remaining():
    q = WindowActionQueue()
    q.enqueue("w1", "interrupt", {})
    q.enqueue("w1", "send", {"text": "x"})
    calls = []

    def boom(payload):
        raise RuntimeError("stop")

    n = q.drain("w1", handlers={"interrupt": boom, "send": lambda p: calls.append("s")})
    assert n == 0
    assert calls == []  # 后续动作被中止
    assert q.peek("w1") == []  # 队列已清空


def test_interrupt_discards_pending_send():
    """交接链路：旧会话排队的 send 不能带进中断后的新会话

    实际时序：auto_compact 先入队补写提示 send(ephemeral)，模型随后入队
    interrupt/new/send(继续指令)。两条 send 都执行时，第一条启动的 worker
    会让第二条撞上 "Already streaming" 回滚，UI 停在发送态而 worker 仍在跑
    （按钮中止不了对话）。
    """
    q = WindowActionQueue()
    q.enqueue("w1", "send", {"text": "补写提示", "ephemeral": True})
    q.enqueue("w1", "interrupt", {})
    q.enqueue("w1", "new", {})
    q.enqueue("w1", "send", {"text": "继续", "ephemeral": False})

    calls = []
    handlers = {
        "interrupt": lambda p: calls.append("i"),
        "new": lambda p: calls.append("n"),
        "send": lambda p: calls.append(("s", p["text"])),
    }
    assert q.drain("w1", handlers=handlers) == 3
    assert calls == ["i", "n", ("s", "继续")]


def test_new_keeps_send_enqueued_after_it():
    """new 之后入队的 send 属于新会话，必须保留"""
    q = WindowActionQueue()
    q.enqueue("w1", "send", {"text": "旧"})
    q.enqueue("w1", "new", {})
    q.enqueue("w1", "send", {"text": "新"})
    assert [i["payload"]["text"] for i in q.peek("w1") if i["action"] == "send"] == ["新"]


def test_windows_isolated():
    q = WindowActionQueue()
    q.enqueue("w1", "new", {})
    assert q.peek("w2") == []


def test_enqueue_rejects_empty_window():
    q = WindowActionQueue()
    assert q.enqueue("", "new", {}) is False


def test_enqueue_rejected_while_draining():
    q = WindowActionQueue()
    q.enqueue("w1", "send", {"text": "a"})
    accepted = []
    result = q.drain("w1", handlers={"send": lambda p: accepted.append(q.enqueue("w1", "send", {"text": "b"}))})
    assert result == 1
    assert accepted == [False]  # 执行期间拒绝入队
