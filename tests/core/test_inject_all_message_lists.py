# -*- coding: utf-8 -*-
"""系统注入消息必须同时落进三条消息流 — 回归测试。

背景（2026-11-27）：「gpt-6 对消息列表里中途插入的消息没反应」。根因是注入
调用点各自只补了一条流：

- 主循环 ``_inject_pending_hook_messages(session_messages_target=current_messages)``
  只写 chat 形态 api cache + messages，会话列表靠 ``_current_session_messages`` 兜；
- 退出前续轮 ``_drain_pending_hooks_before_exit`` 与 PreToolUse 注入只写
  session 列表 + api cache，**不写 current_messages**。

chat/completions 通道读 api cache 所以正常；openai_responses 通道（gpt-6 系
全部）忽略 api_messages、只从 ``create_stream(messages)`` 的入参重新序列化，
于是那两条路径注入的消息 UI 有卡、模型看不到。

修复后统一走 ``_append_injected_messages``，本用例钉住三条流都写且不重复。
"""

import queue
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QObject

from app.plugins.contracts.loop_policy import LoopDecision


class _AlwaysContinuePolicy:
    """退出前续轮恒允许（注入路径专用）"""

    id = "always-continue"

    def should_continue(self, state):
        return LoopDecision.CONTINUE

    def max_rounds(self, llm_config):
        return None


def _make_worker():
    worker_cls = pytest.importorskip("app.core.workers.chat_worker").OpenAIChatWorker
    w = worker_cls.__new__(worker_cls)
    QObject.__init__(w)
    backend = SimpleNamespace(
        _hook_message_queue=queue.Queue(),
        _pre_tool_message_queue=queue.Queue(),
        _window_id=None,
    )
    w.tool_executor = SimpleNamespace(_backend=backend)
    w.session_id = "test-session"
    w._api_messages_cache = []
    w._current_session_messages = []
    w._loop_policy_obj = _AlwaysContinuePolicy()
    w.llm_config = {}
    w._loop_round_count = 0
    w._is_cancelled = False
    w._emit_with_callback = lambda *a, **k: None
    # 序列化器依赖插件注册表，注入测试只关心「是否写进哪些列表」，记录调用即可
    w._api_cache_calls = []
    w._append_to_api_cache = lambda msgs: w._api_cache_calls.extend(msgs)
    return w


def _bind(w, current_messages, current_session_messages):
    """对齐 run() 开头的落点绑定（镜像列表同步为会话列表副本）"""
    w._live_messages = current_messages
    w._live_session_messages = current_session_messages
    w._current_session_messages = list(current_session_messages)


def _contents(msgs):
    return [m.get("content") for m in msgs]


def test_main_loop_inject_writes_all_lists_without_duplicates():
    """主循环注入：调用点把 API 列表当 session 目标传，出口去重后只写一次"""
    w = _make_worker()
    current_messages = [{"role": "user", "content": "hi"}]
    current_session_messages = [{"role": "user", "content": "hi"}]
    _bind(w, current_messages, current_session_messages)

    w.tool_executor._backend._hook_message_queue.put(
        {"role": "user", "content": "[PostToolUse] out", "_hook_event": "PostToolUse"}
    )
    w._inject_pending_hook_messages(session_messages_target=current_messages)

    assert _contents(current_messages) == ["hi", "[PostToolUse] out"]
    assert _contents(current_session_messages) == ["hi", "[PostToolUse] out"]
    assert _contents(w._current_session_messages) == ["hi", "[PostToolUse] out"]
    assert _contents(w._api_cache_calls) == ["[PostToolUse] out"]


def test_pretool_inject_writes_api_messages():
    """PreToolUse 注入：旧实现只写 session 列表，responses 通道看不到"""
    w = _make_worker()
    current_messages = [{"role": "user", "content": "hi"}]
    current_session_messages = [{"role": "user", "content": "hi"}]
    _bind(w, current_messages, current_session_messages)

    w.tool_executor._backend._pre_tool_message_queue.put(
        {"role": "user", "content": "[PreToolUse] out", "_hook_event": "PreToolUse"}
    )
    w._inject_pending_pretool_messages(session_messages_target=current_session_messages)

    assert _contents(current_messages) == ["hi", "[PreToolUse] out"]
    assert _contents(current_session_messages) == ["hi", "[PreToolUse] out"]


def test_exit_drain_inject_writes_api_messages():
    """退出前续轮注入：旧实现同样漏写 API 列表（本轮主要症状来源）"""
    w = _make_worker()
    current_messages = [{"role": "user", "content": "hi"}]
    current_session_messages = [{"role": "user", "content": "hi"}]
    _bind(w, current_messages, current_session_messages)

    w.tool_executor._backend._hook_message_queue.put(
        {"role": "user", "content": "[SubAgentFinished] out", "_hook_event": "SubAgentFinished"}
    )
    assert w._drain_pending_hooks_before_exit(current_session_messages) is True

    assert _contents(current_messages) == ["hi", "[SubAgentFinished] out"]
    assert _contents(current_session_messages) == ["hi", "[SubAgentFinished] out"]


def test_inject_before_binding_degrades_without_crash():
    """run() 绑定前（未进入主循环）注入：退化为只写 api cache + 显式目标，不炸"""
    w = _make_worker()
    w._live_messages = None
    w._live_session_messages = None
    target = [{"role": "user", "content": "hi"}]

    w.tool_executor._backend._hook_message_queue.put(
        {"role": "user", "content": "[PostToolUse] out", "_hook_event": "PostToolUse"}
    )
    w._inject_pending_hook_messages(session_messages_target=target)

    assert _contents(target) == ["hi", "[PostToolUse] out"]
