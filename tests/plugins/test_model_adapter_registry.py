# -*- coding: utf-8 -*-
"""ModelAdapterRegistry：注册/解析/覆盖/清理 + 兜底行为"""
import pytest

from app.plugins.contracts.model_adapter import ModelAdapter, ProtocolFlags


class _FakeAdapter:
    def __init__(self, id: str, score: int, flags: ProtocolFlags = None):
        self.id = id
        self._score = score
        self._flags = flags or ProtocolFlags()

    def matches(self, llm_config: dict) -> int:
        return self._score

    def protocol_flags(self, llm_config: dict) -> ProtocolFlags:
        return self._flags


@pytest.fixture()
def fresh_registry(monkeypatch):
    """每用例独立 registry（绕过单例状态污染）"""
    from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry

    reg = ModelAdapterRegistry()
    monkeypatch.setattr(ModelAdapterRegistry, "get_instance", staticmethod(lambda: reg))
    return reg


def test_fallback_when_empty(fresh_registry):
    """空注册表 → 兜底 adapter（全 False flags），不抛异常"""
    ad = fresh_registry.resolve({"模型名称": "gemini-2.5-pro"})
    assert ad.protocol_flags({}) == ProtocolFlags()


def test_highest_score_wins(fresh_registry):
    fresh_registry.register(_FakeAdapter("a", 1, ProtocolFlags(use_responses_api=True)))
    fresh_registry.register(_FakeAdapter("b", 5, ProtocolFlags(is_gemini=True)))
    flags = fresh_registry.resolve({}).protocol_flags({})
    assert flags.is_gemini is True and flags.use_responses_api is False


def test_zero_score_not_matched(fresh_registry):
    """matches 返回 0 = 不匹配 → 走兜底"""
    fresh_registry.register(_FakeAdapter("a", 0, ProtocolFlags(is_gemini=True)))
    assert fresh_registry.resolve({}).protocol_flags({}) == ProtocolFlags()


def test_unregister_source(fresh_registry):
    fresh_registry.register(_FakeAdapter("a", 5), source="plugin:demo")
    fresh_registry.unregister_source("plugin:demo")
    assert fresh_registry.resolve({}).protocol_flags({}) == ProtocolFlags()


def test_protocol_runtime_checkable():
    assert isinstance(_FakeAdapter("x", 1), ModelAdapter)