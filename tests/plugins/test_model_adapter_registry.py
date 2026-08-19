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
    """空注册表 → resolve 返回 None（去硬编码兜底，调用方显式引导加载系统插件）"""
    assert fresh_registry.resolve({"模型名称": "gemini-2.5-pro"}) is None


def test_highest_score_wins(fresh_registry):
    fresh_registry.register(_FakeAdapter("a", 1, ProtocolFlags(use_responses_api=True)))
    fresh_registry.register(_FakeAdapter("b", 5, ProtocolFlags(is_gemini=True)))
    flags = fresh_registry.resolve({}).protocol_flags({})
    assert flags.is_gemini is True and flags.use_responses_api is False


def test_zero_score_not_matched(fresh_registry):
    """matches 返回 0 = 不匹配 → resolve 返回 None"""
    fresh_registry.register(_FakeAdapter("a", 0, ProtocolFlags(is_gemini=True)))
    assert fresh_registry.resolve({}) is None


def test_unregister_source(fresh_registry):
    fresh_registry.register(_FakeAdapter("a", 5), source="plugin:demo")
    fresh_registry.unregister_source("plugin:demo")
    assert fresh_registry.resolve({}) is None


def test_protocol_runtime_checkable():
    assert isinstance(_FakeAdapter("x", 1), ModelAdapter)


def test_register_overrides_same_id(fresh_registry):
    """同 id 重复注册 → 后者覆盖前者（spec 明文语义）"""
    first = _FakeAdapter("a", 9, ProtocolFlags(is_gemini=True))
    second = _FakeAdapter("a", 9, ProtocolFlags(is_gemini=False))
    fresh_registry.register(first)
    fresh_registry.register(second)
    assert fresh_registry.adapters()["a"] is second
    assert fresh_registry.resolve({}).protocol_flags({}).is_gemini is False


def test_matches_exception_skipped(fresh_registry):
    """matches 拖异常 → 该 adapter 被跳过不拖垮 resolve"""

    class _BrokenAdapter:
        id = "broken"

        def matches(self, llm_config):
            raise RuntimeError("boom")

        def protocol_flags(self, llm_config):
            return ProtocolFlags()

    fresh_registry.register(_BrokenAdapter())
    fresh_registry.register(_FakeAdapter("good", 2, ProtocolFlags(use_responses_api=True)))
    # broken 拖异常被跳过，good 生效
    assert fresh_registry.resolve({}).protocol_flags({}).use_responses_api is True
