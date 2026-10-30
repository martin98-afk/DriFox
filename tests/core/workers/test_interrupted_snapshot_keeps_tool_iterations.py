# -*- coding: utf-8 -*-
"""回归测试：强停中断快照必须包含本轮已完成的工具迭代消息。

用户场景（2026-10-30 实测丢历史）：
  流式中插话发送 → 强制停止 → 撤回插话卡片 → 继续发送
  → 前一轮全部工具调用 / 大模型调用从对话里消失。

根因链：
  1. ``ChatWorkerState.session.current_messages`` 只在构造期写入 run 起点
     快照，运行中从不随工具迭代推进；
  2. ``_sync_state_from_state()``（每轮 API 调用前的
     ``_clear_pending_response_state`` 以及工具执行后都会调）用
     ``s.session.current_messages`` 反向覆盖 ``_current_session_messages``，
     把刚推进的列表回退成起点快照；
  3. 强停时 ``finalize_stop()`` 在 worker 仍卡在流式读取期间调用
     ``get_interrupted_messages()``，读到的正是被回退的起点列表 + 当前
     partial → 本轮 assistant(tool_calls)/tool 全丢；
  4. 残缺快照经 ``_apply_interrupted_messages_to_session`` 覆盖 session，
     用户继续发送时新 worker 以残缺历史为基线，前面内容彻底消失。

修复：会话消息改为单向同步（初始化读 state，之后以 worker 实例为准）。

本测试用 ``__new__`` 桩装配真实 state 与方法（不启动线程、不发请求）。
"""

from app.core.workers.chat_worker import OpenAIChatWorker
from app.core.workers.chat_worker_state import ChatWorkerState

_TOOL_CALL_ASSISTANT = {
    "role": "assistant",
    "content": "",
    "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
    ],
}
_TOOL_RESULT = {"role": "tool", "tool_call_id": "call_1", "name": "bash", "content": "ok"}


def _make_worker(session_messages):
    """装配 __new__ 桩：真实 ChatWorkerState + 最小运行时字段"""
    w = OpenAIChatWorker.__new__(OpenAIChatWorker)
    w._state = ChatWorkerState.from_constructor_args(
        messages=list(session_messages),
        session_messages=list(session_messages),
        llm_config={},
        tools=[],
        permission_cache=None,
        event_bus=None,
        tool_executor=None,
        initial_compaction_cache=None,
    )
    w.session_messages = list(session_messages)
    w.messages = list(session_messages)
    w._current_session_messages = list(session_messages)
    w._sync_state_from_state()
    # _build_response_message_sequence 直接读的属性（真实构造由 __init__ 写入）
    w._cached_model_name = "test-model"
    w._last_context_token_count = 0
    return w


def test_clear_pending_state_keeps_iterated_messages():
    """工具迭代后调 _clear_pending_response_state，会话消息不得回退到起点。"""
    base = [{"role": "user", "content": "发布版本"}]
    w = _make_worker(base)
    assert len(w._current_session_messages) == 1

    # 一轮工具迭代完成后的运行态：user + assistant(tool_calls) + tool
    iterated = list(base) + [_TOOL_CALL_ASSISTANT, _TOOL_RESULT]
    w._current_session_messages = list(iterated)

    # 下一轮 API 调用前（_clear_pending_response_state 内部会 _sync_state_from_state）
    w._clear_pending_response_state()

    assert len(w._current_session_messages) == len(iterated), (
        f"会话消息被状态同步回退到 run 起点快照：期望 {len(iterated)} 条，实际 {len(w._current_session_messages)} 条"
    )
    assert any(m.get("role") == "tool" for m in w._current_session_messages)


def test_interrupted_snapshot_contains_tool_iterations():
    """强停快照（finalize_stop → get_interrupted_messages）必须含工具迭代。"""
    base = [{"role": "user", "content": "发布版本"}]
    w = _make_worker(base)

    iterated = list(base) + [_TOOL_CALL_ASSISTANT, _TOOL_RESULT]
    w._current_session_messages = list(iterated)
    w._clear_pending_response_state()

    snapshot = w.get_interrupted_messages()

    roles = [m.get("role") for m in snapshot]
    assert any(r == "tool" for r in roles), f"中断快照丢失工具结果：roles={roles}"
    assert sum(1 for r in roles if r == "assistant") >= 1, f"中断快照丢失 assistant：roles={roles}"
    assert len(snapshot) >= len(iterated), f"快照条数 {len(snapshot)} < 运行态 {len(iterated)}"


def test_state_session_messages_follows_worker_after_sync():
    """同步后 state.session.current_messages 应跟随实例值（其他读 state 的路径同口径）。"""
    base = [{"role": "user", "content": "hi"}]
    w = _make_worker(base)

    iterated = list(base) + [_TOOL_CALL_ASSISTANT, _TOOL_RESULT]
    w._current_session_messages = list(iterated)
    w._sync_state_from_state()

    assert w._state.session.current_messages is w._current_session_messages, (
        "state.session.current_messages 未跟随实例值，后续反向同步会再次回退"
    )
