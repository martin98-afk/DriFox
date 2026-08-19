# -*- coding: utf-8 -*-
"""SerializerRegistry：注册/覆盖/回退/无 openai 抛错 + MessageSerializer 契约"""

import pytest

from app.plugins.contracts.message_serializer import MessageSerializer, SerializeContext
from app.plugins.contracts.model_adapter import ProtocolFlags


class _FakeSerializer:
    def __init__(self, id: str = "openai"):
        self.id = id
        self.calls = []

    def serialize_messages(self, messages, ctx: SerializeContext):
        self.calls.append(("messages", ctx))
        return [{"role": "user", "content": f"fake-{self.id}"}]

    def serialize_responses(self, messages, ctx: SerializeContext):
        self.calls.append(("responses", ctx))
        return ([{"type": "message", "role": "user", "content": []}], "")


@pytest.fixture()
def fresh_registry(monkeypatch):
    """每用例独立 registry（绕过单例状态污染，对齐 test_model_adapter_registry）"""
    from app.plugins.registries.serializer_registry import SerializerRegistry

    reg = SerializerRegistry()
    monkeypatch.setattr(SerializerRegistry, "get_instance", staticmethod(lambda: reg))
    return reg


def test_resolve_default_id(fresh_registry):
    """注册 openai → 不传 id resolve 返回 openai"""
    s = _FakeSerializer("openai")
    fresh_registry.register(s)
    assert fresh_registry.resolve() is s


def test_resolve_by_id(fresh_registry):
    """注册多 id → 按 id 精确解析"""
    a = _FakeSerializer("openai")
    b = _FakeSerializer("gemini")
    fresh_registry.register(a)
    fresh_registry.register(b)
    assert fresh_registry.resolve("gemini") is b
    assert fresh_registry.resolve("openai") is a


def test_resolve_fallback_to_openai(fresh_registry):
    """无该 id → 回退 openai"""
    a = _FakeSerializer("openai")
    fresh_registry.register(a)
    assert fresh_registry.resolve("nonexistent") is a


def test_resolve_empty_raises(fresh_registry):
    """注册表空且无 openai → 抛错（零硬编码兜底）"""
    with pytest.raises(RuntimeError):
        fresh_registry.resolve()


def test_register_overrides_same_id(fresh_registry):
    """同 id 重复注册 → 后者覆盖（插件替换语义）"""
    first = _FakeSerializer("openai")
    second = _FakeSerializer("openai")
    fresh_registry.register(first)
    fresh_registry.register(second)
    assert fresh_registry.resolve() is second


def test_unregister_source(fresh_registry):
    """unregister_source 清理后回退/抛错"""
    fresh_registry.register(_FakeSerializer("openai"), source="plugin:demo")
    fresh_registry.unregister_source("plugin:demo")
    with pytest.raises(RuntimeError):
        fresh_registry.resolve()


def test_protocol_runtime_checkable():
    """MessageSerializer 可被 isinstance 探测（能力探测语义）"""
    assert isinstance(_FakeSerializer("openai"), MessageSerializer)


def test_serialize_context_defaults():
    """SerializeContext 默认值：supports_vision=True，flags 全 False + serializer_id=openai"""
    ctx = SerializeContext()
    assert ctx.supports_vision is True
    assert ctx.flags.is_gemini is False
    assert ctx.flags.requires_reasoning_content is False
    assert ctx.flags.use_responses_api is False
    assert ctx.flags.serializer_id == "openai"


def test_protocol_flags_serializer_id_default():
    """ProtocolFlags 新增 serializer_id 默认 openai（本阶段只立不消费）"""
    flags = ProtocolFlags()
    assert flags.serializer_id == "openai"
    assert ProtocolFlags(is_gemini=True).serializer_id == "openai"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
