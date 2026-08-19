# -*- coding: utf-8 -*-
"""message_content 薄壳委托测试：to_api_message / messages_to_api /
messages_to_responses_input 经 SerializerRegistry 委托，调用点零改动行为等价。

关键不变量：
- 返回形态不变（List[Dict] / 单条 Dict / (input_items, instructions) tuple）
- to_api_message 旧语义保持（normalize 失败 → {}；空 user → {"role":"user","content":""}）
"""

import pytest

from app.core import message_content as mc
from app.plugins.contracts.message_serializer import SerializeContext
from app.plugins.contracts.model_adapter import ProtocolFlags


class _MockSerializer:
    id = "openai"

    def __init__(self):
        self.messages_calls = []
        self.responses_calls = []

    def serialize_messages(self, messages, ctx: SerializeContext):
        self.messages_calls.append((messages, ctx))
        return [{"role": "user", "content": "MOCK"}]

    def serialize_responses(self, messages, ctx: SerializeContext):
        self.responses_calls.append((messages, ctx))
        return ([{"type": "message", "role": "user", "content": []}], "MOCK-INSTR")


@pytest.fixture()
def fresh_registry(monkeypatch):
    """每用例独立 registry（绕过单例状态污染）"""
    from app.plugins.registries.serializer_registry import SerializerRegistry

    reg = SerializerRegistry()
    monkeypatch.setattr(SerializerRegistry, "get_instance", staticmethod(lambda: reg))
    return reg


class TestDelegation:
    def test_messages_to_api_delegates(self, fresh_registry):
        """messages_to_api → registry 委托（mock serializer 结果透传 + 上下文正确）"""
        mock = _MockSerializer()
        fresh_registry.register(mock)
        msgs = [{"role": "user", "content": "hi"}]
        result = mc.messages_to_api(msgs, supports_vision=False, is_gemini=True, requires_reasoning_content=True)
        assert result == [{"role": "user", "content": "MOCK"}]
        assert len(mock.messages_calls) == 1
        sent, ctx = mock.messages_calls[0]
        assert sent == msgs
        assert ctx.supports_vision is False
        assert ctx.flags.is_gemini is True
        assert ctx.flags.requires_reasoning_content is True
        assert ctx.flags.use_responses_api is False

    def test_to_api_message_delegates(self, fresh_registry):
        """to_api_message → registry 委托（单条 Dict 返回形态不变）"""
        mock = _MockSerializer()
        fresh_registry.register(mock)
        result = mc.to_api_message({"role": "user", "content": "hi"}, is_gemini=True)
        assert result == {"role": "user", "content": "MOCK"}

    def test_responses_delegates(self, fresh_registry):
        """messages_to_responses_input → serialize_responses 委托（tuple 形态不变）"""
        mock = _MockSerializer()
        fresh_registry.register(mock)
        msgs = [{"role": "user", "content": "hi"}]
        result = mc.messages_to_responses_input(msgs, supports_vision=False)
        assert result == ([{"type": "message", "role": "user", "content": []}], "MOCK-INSTR")
        assert len(mock.responses_calls) == 1
        sent, ctx = mock.responses_calls[0]
        assert sent == msgs
        assert ctx.supports_vision is False
        assert ctx.flags.use_responses_api is True

    def test_unregister_falls_back_to_cold_start(self, fresh_registry):
        """mock 卸载后注册表空 → 冷启动幂等加载系统插件 openai，行为回到默认"""
        mock = _MockSerializer()
        fresh_registry.register(mock, source="plugin:demo")
        fresh_registry.unregister_source("plugin:demo")
        result = mc.messages_to_api([{"role": "system", "content": "s"}])
        assert result == [{"role": "system", "content": "s"}]


class TestLegacySemantics:
    def test_to_api_message_normalize_fail_returns_empty(self):
        """normalize 失败 → {}（旧语义）"""
        assert mc.to_api_message({"role": "weird", "content": None, "unknown": 1}) == {}
        assert mc.to_api_message(None) == {}

    def test_to_api_message_empty_user_compat(self):
        """空 user 消息 → {"role":"user","content":""}（旧 to_api_message 语义，不过滤）"""
        result = mc.to_api_message({"role": "user", "content": ""})
        assert result == {"role": "user", "content": ""}

    def test_messages_to_api_skips_empty_user(self):
        """messages_to_api 过滤空 user（列表语义与单条不同）"""
        result = mc.messages_to_api([{"role": "user", "content": ""}, {"role": "system", "content": "s"}])
        assert result == [{"role": "system", "content": "s"}]

    def test_responses_returns_tuple(self):
        """responses 返回 (list, str) tuple（worker :1536 解包依赖）"""
        result = mc.messages_to_responses_input([{"role": "user", "content": "x"}])
        assert isinstance(result, tuple) and len(result) == 2
        assert isinstance(result[0], list) and isinstance(result[1], str)

    def test_exports_unchanged(self):
        """导出符号保留（LazyLoader 兼容）"""
        assert callable(mc.to_api_message)
        assert callable(mc.messages_to_api)
        assert callable(mc.messages_to_responses_input)


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
