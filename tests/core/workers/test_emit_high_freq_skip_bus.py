# -*- coding: utf-8 -*-
"""回归测试：高频流式事件不再空跑 WorkerEventBus（T-A）。

背景：_emit_with_callback 历史上对每个事件同时走 event_bus.emit 与
signal.emit；全仓无生产代码订阅 WorkerEventBus，而 bus.emit 即使零订阅者
也要获取 threading.Lock——流式期 content/reasoning/tool_args 每 chunk
一次的锁竞争是纯开销。

修复：高频流式事件（CONTENT_RECEIVED / REASONING_RECEIVED / TOOL_CALL_STREAM）
跳过 bus 分发，仅保留 signal.emit；生命周期类事件保持双路径不变。

本测试用 __new__ 桩装配（不启动线程、不发请求）。
"""

from app.core.workers.chat_worker import OpenAIChatWorker
from app.core.workers.worker_event_bus import WorkerEvent, WorkerEventBus

def _make_worker_with_bus():
    w = OpenAIChatWorker.__new__(OpenAIChatWorker)
    w._event_bus = WorkerEventBus()
    return w

def test_high_freq_stream_events_skip_bus():
    """content/reasoning/tool_args 高频事件不进 bus，handler 不得被触发。"""
    w = _make_worker_with_bus()
    seen = []
    for name in (
        WorkerEvent.CONTENT_RECEIVED,
        WorkerEvent.REASONING_RECEIVED,
        WorkerEvent.TOOL_CALL_STREAM,
    ):
        w._event_bus.subscribe(name, lambda *a: seen.append(a))
    # signal 传 None：本用例只验证 bus 分发行为，不走 PyQt 信号
    w._emit_with_callback("content_received", None, "chunk1")
    w._emit_with_callback("reasoning_content_received", None, "think")
    w._emit_with_callback("tool_args_updated", None, "call_1", "bash", "{}", "args")
    assert seen == []

def test_low_freq_events_keep_bus_path():
    """生命周期类事件保持双路径：bus handler 仍应收到分发。"""
    w = _make_worker_with_bus()
    seen = []
    w._event_bus.subscribe(WorkerEvent.ERROR, lambda *a: seen.append(a))
    w._emit_with_callback("error_occurred", None, "boom")
    assert seen == [("boom",)]
