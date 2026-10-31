# -*- coding: utf-8 -*-
"""验证：空闲期 PluginChanged 入队 → 下一次对话 worker.run() 第一轮补注入。

复现用户报告场景：
1. 无对话进行时插件变更 hook 触发，输出进 backend._hook_message_queue
2. 用户发起新对话 → 新 worker run()
3. 断言：PluginChanged 消息注入 API 缓存与 session messages
"""

import queue
from types import SimpleNamespace

import pytest

from app.core.workers.chat_worker import OpenAIChatWorker


class _SignalStub:
    """零依赖信号桩：记录 emit 调用"""

    def __init__(self):
        self.emitted = []

    def emit(self, *args):
        self.emitted.append(args)


def _make_worker(hook_queue: queue.Queue) -> OpenAIChatWorker:
    w = OpenAIChatWorker.__new__(OpenAIChatWorker)  # 绕过 __init__
    w.messages = [{"role": "user", "content": "你好"}]
    w.session_messages = [{"role": "user", "content": "你好"}]
    w.llm_config = {"API_KEY": "k", "API_URL": "https://x/v1", "模型名称": "gpt-4o"}
    w.tools = None
    w.session_id = "sess_test"
    w._is_cancelled = False
    w._last_compaction_state = None
    w._state = SimpleNamespace(
        api_cache=SimpleNamespace(cache=None),
        session=SimpleNamespace(last_usage=None),
    )
    w._permission_cache = SimpleNamespace(clear_round=lambda: None)
    w._response_chunks = ["好的"]
    w._response_content_blocks = []
    w._stop_hook_active = False
    w._prev_stophook_response = None
    w._last_usage = {"prompt_tokens": 500}
    w.context_updated = _SignalStub()
    w.finished_with_messages = _SignalStub()
    w.finished_with_content = _SignalStub()
    w.isInterruptionRequested = lambda: False
    # 依赖桩：只关心 hook 队列注入这一段
    w._emit_compaction_status = lambda state: None
    w._clear_pending_response_state = lambda: None
    w._mem_snapshot = lambda *a, **k: None
    w._check_loop_round_limit = lambda: False
    w._trigger_worker_hook = lambda *a, **k: None
    w._emit_with_callback = lambda name, sig, *a: sig.emit(*a) if hasattr(sig, "emit") else None

    def _fake_api_call(messages, use_cache=True):
        # 记录实际发给 LLM 的消息（含注入）
        w._seen_api_messages = [list(messages), getattr(w, "_api_messages_cache", None)]
        return 0, None

    w._make_api_call = _fake_api_call

    def _fake_response_sequence():
        return [{"role": "assistant", "content": "好的"}]

    w._build_response_message_sequence = _fake_response_sequence
    # backend：带 _hook_message_queue（无 _window_id → 跳过 TeamMail）
    w.tool_executor = SimpleNamespace(_backend=SimpleNamespace(_hook_message_queue=hook_queue))
    return w


def test_idle_plugin_changed_injected_next_conversation():
    """空闲时入队的 PluginChanged 消息 → 下次对话 run() 第一轮注入。"""
    hook_q = queue.Queue()
    hook_q.put(
        {
            "role": "user",
            "content": "<system-reminder><plugin-changed-hook>插件 X 已更新</plugin-changed-hook></system-reminder>",
            "_hook_event": "PluginChanged",
        }
    )
    w = _make_worker(hook_q)

    w.run()

    # 队列被消费
    assert hook_q.empty(), f"队列应被消费，剩余 {hook_q.qsize()} 条"
    # 注入进 session messages（持久化路径）
    session_msgs = w.finished_with_messages.emitted[0][0] if w.finished_with_messages.emitted else []
    assert any("plugin-changed-hook" in str(m.get("content", "")) for m in session_msgs), (
        f"PluginChanged 消息应进 session messages，实际: {[m.get('content', '')[:50] for m in session_msgs]}"
    )
    # 注入进 API 缓存（LLM 请求路径）
    api_cache = w._api_messages_cache
    assert api_cache is not None and any(
        "plugin-changed-hook" in str(m.get("content", "")) for m in api_cache
    ), f"PluginChanged 消息应进 API 缓存，实际: {api_cache}"
