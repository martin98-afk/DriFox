# -*- coding: utf-8 -*-
"""OpenAIChatSerializer：默认序列化器与 message_content 旧函数逐点等价。

行为零变化判据：serialize_messages ≡ messages_to_api（含 to_api_message 全部分支），
serialize_responses ≡ messages_to_responses_input。断言从
tests/core/test_message_content_custom.py 与 tests/test_reasoning_content_required.py 抽样。
"""

import pytest

from app.core import message_content as mc
from app.plugins.contracts.message_serializer import SerializeContext
from app.plugins.contracts.model_adapter import ProtocolFlags
from plugins.system.serializers.openai import OpenAIChatSerializer

SER = OpenAIChatSerializer()


def _ctx(**flags_kwargs):
    return SerializeContext(flags=ProtocolFlags(**flags_kwargs))


# ---------- chat/completions 形态 ----------


def test_system_message():
    msg = {"role": "system", "content": "你是助手"}
    assert SER.serialize_messages([msg], _ctx()) == mc.messages_to_api([msg])


def test_user_text_and_params_merged():
    msg = {"role": "user", "content": "hi", "params": {"ctx": ["x", "context-内容"]}}
    assert SER.serialize_messages([msg], _ctx()) == mc.messages_to_api([msg])


def test_user_multimodal_vision_off():
    """supports_vision=False → 图片块替换为占位（与旧 to_api_message 一致）"""
    msg = {"role": "user", "content": [{"type": "text", "text": "看"}, {"type": "image_url", "image_url": {"url": "data:img"}}]}
    expected = mc.messages_to_api([msg], supports_vision=False)
    actual = SER.serialize_messages([msg], SerializeContext(supports_vision=False))
    assert actual == expected
    assert "[图片]" in actual[0]["content"]


def test_assistant_tool_calls_reasoning_required():
    """requires_reasoning_content=True → tool_calls assistant 补 reasoning_content 空串"""
    msg = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}
    expected = mc.messages_to_api([msg], requires_reasoning_content=True)
    actual = SER.serialize_messages([msg], _ctx(requires_reasoning_content=True))
    assert actual == expected
    assert actual[0]["reasoning_content"] == ""


def test_assistant_reasoning_preserved():
    msg = {"role": "assistant", "content": "", "reasoning_content": "thinking...", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}
    assert SER.serialize_messages([msg], _ctx()) == mc.messages_to_api([msg])


def test_assistant_gemini_thought_signature():
    """is_gemini=True → tool_calls 注入占位 thought_signature（等价旧 _build_api_tool_call）"""
    msg = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}
    expected = mc.messages_to_api([msg], is_gemini=True)
    actual = SER.serialize_messages([msg], _ctx(is_gemini=True))
    assert actual == expected
    assert "extra_content" in actual[0]["tool_calls"][0]


def test_tool_message_pruned():
    """tool 结果 → content 字符串 + S1 截断（等价旧 _prune_tool_content_for_api）"""
    msg = {"role": "tool", "tool_call_id": "c1", "name": "read", "content": "结果内容"}
    assert SER.serialize_messages([msg], _ctx()) == mc.messages_to_api([msg])


def test_empty_user_message_skipped():
    """空 user 消息被跳过（messages_to_api 语义）"""
    msgs = [{"role": "user", "content": ""}, {"role": "system", "content": "s"}]
    assert SER.serialize_messages(msgs, _ctx()) == mc.messages_to_api(msgs)


# ---------- responses 形态 ----------


def test_responses_instructions_and_items():
    msgs = [
        {"role": "system", "content": "指令1"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "回复", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read", "content": "结果"},
    ]
    expected = mc.messages_to_responses_input(msgs)
    actual = SER.serialize_responses(msgs, _ctx())
    assert actual == expected
    assert actual[1] == "指令1"
    types = [item["type"] for item in actual[0]]
    assert types == ["message", "message", "function_call", "function_call_output"]


def test_responses_image_to_text_when_no_vision():
    """responses 形态 supports_vision=False → 图片块替换为 [图片] 文本"""
    msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:img"}}]}]
    expected = mc.messages_to_responses_input(msgs, supports_vision=False)
    actual = SER.serialize_responses(msgs, SerializeContext(supports_vision=False))
    assert actual == expected
    assert actual[0][0]["content"][0]["text"] == "[图片]"


def test_responses_return_shape_tuple():
    """返回形态必须是 (list, str) tuple（worker :1536 解包依赖）"""
    result = SER.serialize_responses([{"role": "user", "content": "x"}], _ctx())
    assert isinstance(result, tuple) and len(result) == 2
    assert isinstance(result[0], list) and isinstance(result[1], str)


# ---------- 系统插件注册约定 ----------


def test_register_convention():
    """register(registry) 对称约定：注册 id="openai" 的实例"""
    from app.plugins.registries.serializer_registry import SerializerRegistry

    class _FakeReg:
        def __init__(self):
            self.items = []

        def register(self, item, source=""):
            self.items.append(item)

    reg = _FakeReg()
    from plugins.system.serializers import openai as ser_mod

    ser_mod.register(reg)
    assert len(reg.items) == 1
    assert reg.items[0].id == "openai"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
