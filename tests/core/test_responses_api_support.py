# -*- coding: utf-8 -*-
"""Responses API（GPT-5.x 系列）流式解析与消息转换测试。

背景：gpt-5.6-luna 等 GPT-5.x 模型的思考只在 Responses API
（/v1/responses）的 reasoning_summary_text 事件返回，chat/completions
流式 delta 无 reasoning 字段 → chat_worker 走 _process_responses_stream
解析事件并复用既有信号（reasoning_content_received / content_received /
tool_call_started 等）。
"""

import json

import pytest

from app.core.conversation.message_content import messages_to_responses_input
from app.core.workers.chat_worker import OpenAIChatWorker
from app.core.workers.subagent_worker import SubAgentExecutor


# ---------- 消息转换 ----------


class TestMessagesToResponsesInput:
    def test_basic_conversion(self):
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
            {"role": "user", "content": "谢谢"},
        ]
        items, instructions = messages_to_responses_input(messages)
        assert instructions == "你是助手"
        # system 不进 input
        assert all(it.get("role") != "system" for it in items)
        assert items[0] == {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "你好"}]}

    def test_tool_call_conversion(self):
        messages = [
            {"role": "user", "content": "北京天气？"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"北京"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "get_weather", "content": "晴 25度"},
        ]
        items, _ = messages_to_responses_input(messages)
        fc = [it for it in items if it.get("type") == "function_call"]
        out = [it for it in items if it.get("type") == "function_call_output"]
        assert len(fc) == 1
        assert fc[0] == {
            "type": "function_call",
            "call_id": "call_1",
            "name": "get_weather",
            "arguments": '{"city":"北京"}',
        }
        assert out[0] == {"type": "function_call_output", "call_id": "call_1", "output": "晴 25度"}

    def test_image_conversion(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                ],
            }
        ]
        items, _ = messages_to_responses_input(messages, supports_vision=True)
        parts = items[0]["content"]
        assert parts[1] == {"type": "input_image", "image_url": "data:image/png;base64,AAA"}
        # 不支持视觉 → 图片替换占位
        items2, _ = messages_to_responses_input(messages, supports_vision=False)
        parts2 = items2[0]["content"]
        assert parts2[1] == {"type": "input_text", "text": "[图片]"}


# ---------- 流式事件解析 ----------


class FakeEvent:
    """模拟 Responses API 流式事件对象（openai SDK 事件的最小形态）"""

    def __init__(self, etype, **kwargs):
        self.type = etype
        for k, v in kwargs.items():
            setattr(self, k, v)


# ---------- 协议入口单点裁决（chat_worker._resolve_use_responses） ----------

_DISPATCH_CASES = [
    # (协议入口, 使用ResponsesAPI, 期望 use_responses)；基线模型 gpt-4o 自动=False
    (None, None, False),
    (None, True, True),
    (None, False, False),
    ("responses", None, True),
    ("responses", False, False),  # 旧键显式 False 压过新键
    ("transport:openai_chat", None, False),
]

@pytest.mark.parametrize("entry,legacy,expect", _DISPATCH_CASES)
def test_entry_dispatch_matrix(qapp, entry, legacy, expect):
    from app.core.workers.chat_worker import OpenAIChatWorker

    w = OpenAIChatWorker(
        messages=[{"role": "user", "content": "hi"}],
        session_messages=[],
        llm_config={"模型名称": "gpt-4o", "API_URL": "http://fake", "API_KEY": ""},
        stream=True,
    )
    if entry is not None:
        w.llm_config["协议入口"] = entry
    if legacy is not None:
        w.llm_config["使用ResponsesAPI"] = legacy
    assert w._resolve_use_responses(w.llm_config) is expect

def test_entry_dispatch_gpt6_auto(qapp):
    """自动模式下 gpt-6 走 responses（detector 修复经 flags 生效）"""
    from app.core.workers.chat_worker import OpenAIChatWorker

    w = OpenAIChatWorker(
        messages=[{"role": "user", "content": "hi"}],
        session_messages=[],
        llm_config={"模型名称": "gpt-6-luna", "API_URL": "http://fake", "API_KEY": ""},
        stream=True,
    )
    assert w._resolve_use_responses(w.llm_config) is True

def test_get_chat_transport_accepts_id(qapp):
    """手选 id 经注册表解析且返回 worker 包装实例（连接池竞态修复不被绕过）"""
    from app.core.workers.chat_worker import OpenAIChatWorker
    from app.core.workers.stream_sink_context import StreamSinkContext  # noqa: F401 触发插件 warmup 前置
    from tests.core.test_subagent_stream_aggregation import _ensure_plugins_loaded

    _ensure_plugins_loaded()
    w = OpenAIChatWorker(
        messages=[{"role": "user", "content": "hi"}],
        session_messages=[],
        llm_config={"模型名称": "gpt-4o", "API_URL": "http://fake", "API_KEY": ""},
        stream=True,
    )
    t = w._get_chat_transport("openai_chat")
    assert getattr(t, "id", "") == "openai_chat"

    def test_responses_tools_conversion(self):
        tools = [
            {
                "type": "function",
                "function": {"name": "read", "description": "读文件", "parameters": {"type": "object"}},
            }
        ]
        out = SubAgentExecutor._responses_tools(tools)
        assert out == [
            {
                "type": "function",
                "name": "read",
                "description": "读文件",
                "parameters": {"type": "object"},
            }
        ]

# ---------- 协议分派：transport id 映射（Phase 3，responses 插件化） ----------
_DISPATCH_ID_CASES = [
    # (协议入口, 使用ResponsesAPI, 期望 transport id)；基线模型 gpt-4o 自动非 responses
    (None, None, None),
    (None, True, "openai_responses"),
    (None, False, None),
    ("responses", None, "openai_responses"),
    ("responses", False, None),  # 旧键显式 False 压过新键
    ("transport:openai_chat", None, "openai_chat"),
]


@pytest.mark.parametrize("entry,legacy,expect", _DISPATCH_ID_CASES)
def test_resolve_transport_id_matrix(qapp, entry, legacy, expect):
    from app.core.workers.chat_worker import OpenAIChatWorker

    w = OpenAIChatWorker(
        messages=[{"role": "user", "content": "hi"}],
        session_messages=[],
        llm_config={"模型名称": "gpt-4o", "API_URL": "http://fake", "API_KEY": ""},
        stream=True,
    )
    if entry is not None:
        w.llm_config["协议入口"] = entry
    if legacy is not None:
        w.llm_config["使用ResponsesAPI"] = legacy
    assert w._resolve_transport_id(w.llm_config) == expect


def test_resolve_transport_id_gpt6_auto(qapp):
    """gpt-6 自动判定 → 映射到 openai_responses 插件"""
    from app.core.workers.chat_worker import OpenAIChatWorker

    w = OpenAIChatWorker(
        messages=[{"role": "user", "content": "hi"}],
        session_messages=[],
        llm_config={"模型名称": "gpt-6-luna", "API_URL": "http://fake", "API_KEY": ""},
        stream=True,
    )
    assert w._resolve_transport_id(w.llm_config) == "openai_responses"

