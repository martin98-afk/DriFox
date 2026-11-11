# -*- coding: utf-8 -*-
"""回归测试：流式中途周期落盘（P1）。

背景：流式期间正文只存在于 worker 私有状态与卡片 DOM，``session.messages``
里连 assistant 条目都还没有。进程在这段时间被杀（崩溃/强杀/断电），整轮内容
从头到尾没有可恢复的东西。修复后按时间节流在 session 尾部维护一条带
``_partial`` 标记的 assistant 消息并落盘。

本测试锁定四条行为：
1. 节流窗口内不重复落盘；到期后覆盖同一条 partial（不新增条目）
2. 等待工具结果的 assistant(tool_calls) 尾部不得插入 partial（协议会被破坏）
3. 临时会话 / 非流式 / 会话切换后一律不落盘
4. ``_partial`` 标记能穿过 consolidate_messages，且发送前被剔除
"""
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import app.main_widget as mw
from app.core.context.builder import ContextBudgetAllocator
from app.core.conversation.chat_session import ChatSession, SessionManager
from app.core.conversation.message_content import consolidate_messages


def _make_widget(initial_messages: list, *, is_streaming: bool = True, is_temp: bool = False):
    widget = mw.OpenAIChatToolWindow.__new__(mw.OpenAIChatToolWindow)
    widget.session_manager = SessionManager()
    session = ChatSession(messages=list(initial_messages))
    session.is_temp = is_temp
    widget.session_manager.sessions.append(session)
    widget.session_manager.current_index = 0
    widget.history_manager = MagicMock()
    widget.history_manager.flush_async = MagicMock()
    widget._is_streaming = is_streaming
    widget._is_destroyed = False
    widget._session_switched = False
    widget._session_dirty = False
    widget._last_partial_flush_ts = 0.0
    widget._partial_flush_interval_s = 25.0
    widget._current_assistant_card = None
    # 保存链在 stub 上不可用（依赖大量窗口状态），替换成只置脏标记的空实现
    widget._save_current_session_to_history = lambda: setattr(widget, "_session_dirty", False)
    return widget, session


class _FakeCard:
    """最小卡片替身：只提供 _content_data（周期落盘唯一读取的字段）。"""

    def __init__(self, blocks):
        self._content_data = blocks


def test_partial_flush_appends_then_overwrites_same_message():
    """到期后追加一条 partial；再到期只覆盖它，不新增条目。"""
    widget, session = _make_widget([{"role": "user", "content": "问题"}])
    widget._current_assistant_card = _FakeCard([{"type": "text", "text": "第一段"}])

    widget._maybe_flush_partial_reply()

    assert len(session.messages) == 2
    partial = session.messages[-1]
    assert partial["role"] == "assistant"
    assert partial["_partial"] is True
    assert partial["content"] == "第一段"

    # 内容增长 + 越过节流窗口 → 覆盖同一条
    widget._current_assistant_card = _FakeCard([{"type": "text", "text": "第一段第二段"}])
    widget._last_partial_flush_ts = 0.0
    widget._maybe_flush_partial_reply()

    assert len(session.messages) == 2, "必须覆盖同一条 partial，不得新增消息"
    assert session.messages[-1]["content"] == "第一段第二段"


def test_partial_flush_throttled_within_interval():
    """节流窗口内不重复落盘。"""
    widget, session = _make_widget([{"role": "user", "content": "问题"}])
    widget._current_assistant_card = _FakeCard([{"type": "text", "text": "内容"}])

    widget._maybe_flush_partial_reply()
    widget.history_manager.flush_async.reset_mock()
    widget._current_assistant_card = _FakeCard([{"type": "text", "text": "内容更多"}])

    widget._maybe_flush_partial_reply()

    assert widget.history_manager.flush_async.call_count == 0, "窗口内不得再次落盘"
    assert session.messages[-1]["content"] == "内容", "内容未到期不得更新"


def test_partial_flush_skips_when_waiting_for_tool_result():
    """尾部是 assistant(tool_calls) 等结果时，插入裸文本会破坏 tool 配对协议。"""
    widget, session = _make_widget(
        [
            {"role": "user", "content": "问题"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}],
            },
        ]
    )
    widget._current_assistant_card = _FakeCard([{"type": "text", "text": "调用前的说明"}])

    widget._maybe_flush_partial_reply()

    assert len(session.messages) == 2, "等待工具结果时不得插入 partial"


def test_partial_flush_allowed_after_tool_result():
    """工具已返回（尾部是 tool 消息）后重新允许落盘。"""
    widget, session = _make_widget(
        [
            {"role": "user", "content": "问题"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "bash", "content": "结果"},
        ]
    )
    widget._current_assistant_card = _FakeCard([{"type": "text", "text": "读完了，结论是"}])

    widget._maybe_flush_partial_reply()

    assert len(session.messages) == 4
    assert session.messages[-1]["_partial"] is True


def test_partial_flush_skips_temp_session():
    """临时对话页一律不落盘。"""
    widget, session = _make_widget([{"role": "user", "content": "问题"}], is_temp=True)
    widget._current_assistant_card = _FakeCard([{"type": "text", "text": "内容"}])

    widget._maybe_flush_partial_reply()

    assert len(session.messages) == 1


def test_partial_flush_skips_when_not_streaming():
    widget, session = _make_widget([{"role": "user", "content": "问题"}], is_streaming=False)
    widget._current_assistant_card = _FakeCard([{"type": "text", "text": "内容"}])

    widget._maybe_flush_partial_reply()

    assert len(session.messages) == 1


def test_partial_flush_skips_after_session_switch():
    """会话切换哨兵置位后不得写（会落到别的会话）。"""
    widget, session = _make_widget([{"role": "user", "content": "问题"}])
    widget._session_switched = True
    widget._current_assistant_card = _FakeCard([{"type": "text", "text": "内容"}])

    widget._maybe_flush_partial_reply()

    assert len(session.messages) == 1


def test_partial_flag_survives_consolidate():
    """_partial 必须在 normalize 白名单里，否则崩溃后分不清「说完」与「半截」。"""
    normalized = consolidate_messages(
        [{"role": "assistant", "content": "半截回复", "_partial": True, "timestamp": "2026-11-09 12:00:00"}]
    )
    assert len(normalized) == 1
    assert normalized[0].get("_partial") is True
    assert normalized[0]["content"] == "半截回复"


def test_partial_messages_excluded_from_send_context():
    """发送前必须剔除 _partial：否则把半截话当正常历史发给模型续写。"""
    session = ChatSession(
        messages=[
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "半截回复", "_partial": True},
        ]
    )
    agent_manager = MagicMock()
    agent_manager.get_agent_system_prompt.return_value = "system"
    builder = ContextBudgetAllocator(agent_manager=agent_manager, backend=None, pipeline=None)
    session.system_prompt = "system"

    messages = builder.build_messages(session, {"model": "gpt-4", "context_window": 128000})

    roles_contents = [(m.get("role"), m.get("content")) for m in messages]
    assert not any("半截回复" in str(c) for _, c in roles_contents), f"残缺回复不得进入发送上下文：{roles_contents}"
    assert any(m.get("role") == "user" for m in messages), "用户消息必须保留"
