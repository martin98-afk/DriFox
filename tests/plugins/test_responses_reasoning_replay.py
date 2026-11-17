# -*- coding: utf-8 -*-
"""Responses 形态 reasoning 回传回归测试（DeepSeek thinking mode）。

背景（2026-11-17 实测）：DeepSeek /responses 在工具循环内要求上一轮 assistant 的
reasoning 以 ``reasoning_text`` item 回传，缺则 400::

    The `reasoning_text` in the thinking mode must be passed back to the API.

且该 item 必须排在 ``function_call`` 之前（服务端要求 call 与 output 严格相邻）。
开关走 ProtocolFlags.replay_reasoning_text，只有 DeepSeek 家族置 True。
"""
import importlib.util
from pathlib import Path

import pytest

from app.plugins.contracts.message_serializer import SerializeContext
from app.plugins.contracts.model_adapter import ProtocolFlags

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent / "plugins"


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SER = _load("system-serializers/serializers/openai.py", "openai_serializer_replay_test").OpenAIChatSerializer()
DEEPSEEK_ADAPTER = _load(
    "system-model-adapters/model_adapters/deepseek_family.py", "deepseek_family_replay_test"
).DeepSeekFamilyAdapter()
TRANSPORT_MOD = _load(
    "system-transports/transports/openai_responses.py", "openai_responses_replay_test"
)


def _ctx(replay: bool):
    return SerializeContext(flags=ProtocolFlags(replay_reasoning_text=replay))


def _assistant(tool_calls=True, reasoning="思考过程"):
    msg = {
        "role": "assistant",
        "content": "开始读文件",
        "reasoning_content": reasoning,
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": '{"p":"a.py"}'}}
        ],
    }
    if not tool_calls:
        msg.pop("tool_calls")
    return msg


def test_reasoning_item_before_function_call():
    msgs = [_assistant()]
    items, _ = SER.serialize_responses(msgs, _ctx(replay=True))
    types = [item["type"] for item in items]
    assert types == ["message", "reasoning", "function_call"], types
    reasoning = items[1]
    assert reasoning == {
        "type": "reasoning",
        "summary": [],
        "content": [{"type": "reasoning_text", "text": "思考过程"}],
    }


def test_reasoning_item_disabled_by_default():
    """开关关闭（OpenAI 官方等其它 responses 端点）保持旧形态：无 reasoning item"""
    msgs = [_assistant()]
    items, _ = SER.serialize_responses(msgs, _ctx(replay=False))
    assert [item["type"] for item in items] == ["message", "function_call"]


def test_empty_reasoning_padded():
    """空串垫一个空格：DeepSeek 只认非空 reasoning_text"""
    items, _ = SER.serialize_responses([_assistant(reasoning="")], _ctx(replay=True))
    reasoning = next(item for item in items if item["type"] == "reasoning")
    assert reasoning["content"][0]["text"] == " "


def test_missing_reasoning_field_still_padded():
    """历史消息完全没有 reasoning_content（老数据/通道切换）也要占位，否则上游 400"""
    msg = _assistant()
    msg.pop("reasoning_content")
    items, _ = SER.serialize_responses([msg], _ctx(replay=True))
    reasoning = next(item for item in items if item["type"] == "reasoning")
    assert reasoning["content"][0]["text"] == " "


def test_plain_assistant_no_reasoning_item():
    """纯文本轮次不回传（只补工具轮，避免历史思考重复计费）"""
    items, _ = SER.serialize_responses([_assistant(tool_calls=False)], _ctx(replay=True))
    assert all(item["type"] != "reasoning" for item in items)


def test_deepseek_adapter_enables_replay():
    flags = DEEPSEEK_ADAPTER.protocol_flags(
        {"API_URL": "https://api.deepseek.com", "模型名称": "deepseek-flash", "思考模式": True}
    )
    assert flags.replay_reasoning_text is True
    assert flags.requires_reasoning_content is True


def test_transport_resolves_adapter_flags():
    """transport 从 adapter registry 取 flag：DeepSeek 配置命中回传开关"""
    from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry

    registry = ModelAdapterRegistry.get_instance()
    registry.register(DEEPSEEK_ADAPTER, source="test")
    try:
        flags = TRANSPORT_MOD.OpenAIResponsesTransport._protocol_flags(
            {"API_URL": "https://api.deepseek.com", "模型名称": "deepseek-flash", "思考模式": True}
        )
        assert flags is not None and flags.replay_reasoning_text is True
    finally:
        registry.unregister_source("test")


def test_transport_end_to_end_serialization():
    """transport 全链路（假 client 不联网）：input 里 reasoning 排在 function_call 前"""

    class _Client:
        class responses:
            captured = {}

            @classmethod
            def create(cls, **kwargs):
                cls.captured = kwargs
                return iter(())

    from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry

    registry = ModelAdapterRegistry.get_instance()
    registry.register(DEEPSEEK_ADAPTER, source="test-e2e")
    try:
        transport = TRANSPORT_MOD.OpenAIResponsesTransport()
        transport.create_stream(
            {"API_URL": "https://api.deepseek.com", "模型名称": "deepseek-flash", "思考模式": True},
            [
                {"role": "user", "content": "读一下 a.py"},
                _assistant(),
                {"role": "tool", "tool_call_id": "call_1", "content": "print(1)"},
            ],
            client=_Client(),
        )
        types = [item["type"] for item in _Client.responses.captured["input"]]
        assert types == ["message", "message", "reasoning", "function_call", "function_call_output"], types
    finally:
        registry.unregister_source("test-e2e")
