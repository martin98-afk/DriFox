# -*- coding: utf-8 -*-
"""协议分派单测：resolve_transport_target 的裁决优先级。

回归背景（2026-11-16）：Anthropic 前缀缓存是 opt-in，OpenAI 兼容层无法表达
cache_control 断点，直连官方（认证方式=anthropic）不能再落到默认 openai_chat。
本条兜底优先级低于「协议入口」手选与 adapter 声明，只压过注册表默认值。
"""

from types import SimpleNamespace

from app.core.workers.protocol_dispatch import (
    ANTHROPIC_TRANSPORT_ID,
    RESPONSES_TRANSPORT_ID,
    resolve_transport_target,
)

def _flags(protocol="openai", use_responses_api=False, extra=None):
    return SimpleNamespace(protocol=protocol, use_responses_api=use_responses_api, extra=extra or {})

def test_default_falls_back_to_registry_default():
    assert resolve_transport_target({"模型名称": "gpt-6"}, _flags()) is None

def test_legacy_responses_key_wins():
    cfg = {"使用ResponsesAPI": True, "认证方式": "anthropic"}
    assert resolve_transport_target(cfg, _flags()) == RESPONSES_TRANSPORT_ID

def test_manual_transport_entry_wins_over_anthropic_auth():
    cfg = {"协议入口": "transport:openai_chat", "认证方式": "anthropic"}
    assert resolve_transport_target(cfg, _flags()) == "openai_chat"

def test_anthropic_auth_routes_to_messages_transport():
    assert resolve_transport_target({"认证方式": "anthropic"}, _flags()) == ANTHROPIC_TRANSPORT_ID

def test_anthropic_auth_is_case_insensitive():
    assert resolve_transport_target({"认证方式": "Anthropic"}, None) == ANTHROPIC_TRANSPORT_ID

def test_bearer_auth_keeps_default():
    assert resolve_transport_target({"认证方式": "bearer"}, _flags()) is None

def test_adapter_declared_transport_wins_over_anthropic_auth():
    cfg = {"认证方式": "anthropic"}
    flags = _flags(extra={"transport": "openai_chat"})
    assert resolve_transport_target(cfg, flags) == "openai_chat"
