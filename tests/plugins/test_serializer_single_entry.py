# -*- coding: utf-8 -*-
"""序列化单入口测试：MessageSerializer.serialize → SerializeResult（chat/responses 双形态）

不变量：
- serialize(chat ctx).messages == 旧 messages_to_api 输出（逐点等价）
- serialize(responses ctx).input_items/instructions == 旧 messages_to_responses_input 输出
- worker 经 flags.serializer_id 解析序列化器（serializer_id 真正被消费）
"""

import pytest

from app.core import message_content as mc
from app.plugins.contracts.message_serializer import SerializeContext, SerializeResult
from app.plugins.contracts.model_adapter import ProtocolFlags
from plugins.system.serializers.openai import OpenAIChatSerializer

SER = OpenAIChatSerializer()

_CHAT_MSGS = [
    {"role": "system", "content": "指令"},
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "回复", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "read", "content": "结果"},
]


def _ctx(**flags_kwargs):
    return SerializeContext(flags=ProtocolFlags(**flags_kwargs))


class TestSingleEntryEquivalence:
    def test_chat_route_equals_messages_to_api(self):
        """chat 形态（use_responses_api=False）→ result.messages 逐点等价旧 messages_to_api"""
        ctx = _ctx(is_gemini=True, requires_reasoning_content=True)
        result = SER.serialize(_CHAT_MSGS, ctx)
        assert isinstance(result, SerializeResult)
        assert result.messages == mc.messages_to_api(
            _CHAT_MSGS, is_gemini=True, requires_reasoning_content=True
        )
        assert result.input_items == [] and result.instructions == ""

    def test_responses_route_equals_old(self):
        """responses 形态（use_responses_api=True）→ input_items/instructions 等价旧函数"""
        ctx = _ctx(use_responses_api=True)
        result = SER.serialize(_CHAT_MSGS, ctx)
        input_items, instructions = mc.messages_to_responses_input(_CHAT_MSGS)
        assert result.input_items == input_items
        assert result.instructions == instructions
        assert result.messages == []

    def test_serialize_defaults(self):
        """SerializeResult 默认值：messages/input_items 空列表，instructions 空串"""
        r = SerializeResult()
        assert r.messages == [] and r.input_items == [] and r.instructions == ""

    def test_serialize_messages_still_available(self):
        """serialize_messages/serialize_responses 保留为公开方法（插件复用）"""
        ctx = _ctx()
        assert SER.serialize_messages(_CHAT_MSGS, ctx) == SER.serialize(_CHAT_MSGS, ctx).messages


class TestWorkerSingleEntry:
    @pytest.fixture()
    def fresh_serializer_registry(self, monkeypatch):
        from app.plugins.registries.serializer_registry import SerializerRegistry

        reg = SerializerRegistry()
        monkeypatch.setattr(SerializerRegistry, "get_instance", staticmethod(lambda: reg))
        return reg

    @pytest.fixture()
    def fresh_adapter_registry(self, monkeypatch):
        from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry

        reg = ModelAdapterRegistry()
        monkeypatch.setattr(ModelAdapterRegistry, "get_instance", staticmethod(lambda: reg))
        return reg

    def test_worker_builds_cache_via_single_entry(self, fresh_serializer_registry, fresh_adapter_registry):
        """worker _build_api_messages_cache 走单入口；serializer_id 消费（demo 序列化器生效）"""

        class _DemoSerializer:
            id = "demo"

            def serialize(self, messages, ctx: SerializeContext):
                return SerializeResult(messages=[{"role": "user", "content": "DEMO" + str(ctx.flags.serializer_id)}])

        class _DemoAdapter:
            id = "demo-family"

            def matches(self, llm_config):
                return 9

            def protocol_flags(self, llm_config):
                return ProtocolFlags(serializer_id="demo")

        fresh_serializer_registry.register(_DemoSerializer(), source="plugin:demo")
        fresh_adapter_registry.register(_DemoAdapter(), source="plugin:demo")
        from app.core.workers.chat_worker import OpenAIChatWorker

        worker = OpenAIChatWorker(
            messages=[{"role": "user", "content": "hi"}],
            session_messages=[],
            llm_config={"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-4o"},
        )
        cache = worker._build_api_messages_cache()
        assert cache == [{"role": "user", "content": "DEMOdemo"}], "serializer_id 应被 adapter 指定并消费（demo 序列化器生效）"

    def test_worker_responses_kwargs_via_single_entry(self, fresh_serializer_registry, fresh_adapter_registry):
        """worker _build_responses_kwargs 走单入口取 input_items/instructions"""

        class _DemoSerializer:
            id = "demo"

            def serialize(self, messages, ctx: SerializeContext):
                return SerializeResult(input_items=[{"type": "message", "role": "user", "content": []}], instructions="DEMO-INSTR")

        class _DemoAdapter:
            id = "demo-family"

            def matches(self, llm_config):
                return 9

            def protocol_flags(self, llm_config):
                return ProtocolFlags(serializer_id="demo", use_responses_api=True)

        fresh_serializer_registry.register(_DemoSerializer(), source="plugin:demo")
        fresh_adapter_registry.register(_DemoAdapter(), source="plugin:demo")
        from app.core.workers.chat_worker import OpenAIChatWorker

        worker = OpenAIChatWorker(
            messages=[{"role": "user", "content": "hi"}],
            session_messages=[],
            llm_config={"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-5.6-luna"},
        )
        kwargs = worker._build_responses_kwargs([{"role": "user", "content": "hi"}])
        assert kwargs["input"] == [{"type": "message", "role": "user", "content": []}]
        assert kwargs["instructions"] == "DEMO-INSTR"

    def test_default_path_unchanged(self, fresh_serializer_registry, fresh_adapter_registry):
        """默认路径：无 demo 适配器 → openai 序列化器，行为与薄壳前等价"""
        from app.plugins.loaders.runtime_component_loader import warmup_runtime_components

        warmup_runtime_components()
        from app.core.workers.chat_worker import OpenAIChatWorker

        worker = OpenAIChatWorker(
            messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
            session_messages=[],
            llm_config={"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-4o"},
        )
        assert worker._build_api_messages_cache() == [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
