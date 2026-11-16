# -*- coding: utf-8 -*-
"""anthropic_messages transport 单测：build_request 思考参数分代形状。

回归背景（2026-11-16）：claude-sonnet-5-5 开思考发 thinking.type=enabled 被
服务端 400（"Use thinking.type.adaptive and output_config.effort"）。
协议事实（platform.claude.com thinking-troubleshooting）：
- Claude 4.5 及以下：仅 enabled+budget_tokens，adaptive 反而 400
- Claude 4.6：两种都收（enabled 已弃用，优先 adaptive）
- Claude 4.7+ / 5.x：仅 adaptive，enabled 400；effort 走 output_config.effort

插件目录非包，按路径 importlib 加载（对齐运行时真实加载方式）。
"""

import importlib.util
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parent.parent.parent / "plugins" / "system-transports"
_MODULE_PATH = _PLUGIN / "transports" / "anthropic_messages.py"


def _load():
    spec = importlib.util.spec_from_file_location("anthropic_messages_transport_test", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load()


@pytest.fixture(scope="module")
def transport(mod):
    return mod.AnthropicMessagesTransport()


def _cfg(model, **extra):
    cfg = {"模型名称": model, "思考模式": True}
    cfg.update(extra)
    return cfg


@pytest.fixture(autouse=True)
def _isolate_caps(monkeypatch, mod):
    """单测不依赖 models.dev 缓存/插件注册表：caps 默认空，按需覆写。"""
    monkeypatch.setattr(mod, "resolve_model_capabilities", lambda cfg: {}, raising=False)


# ---------- 1. 新模型（4.7+/5.x）：adaptive + output_config.effort ----------


def test_claude_5_5_uses_adaptive_with_effort(transport):
    body = transport.build_request(_cfg("claude-sonnet-5-5", **{"思考等级": "high"}))
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high"}


def test_adaptive_omits_effort_when_unset(transport):
    body = transport.build_request(_cfg("claude-sonnet-5-5"))
    assert body["thinking"] == {"type": "adaptive"}
    assert "output_config" not in body


def test_adaptive_ignores_budget_and_keeps_max_tokens(transport):
    body = transport.build_request(
        _cfg("claude-opus-5-5", **{"思考预算": 8192, "最大输出": 4096})
    )
    assert body["thinking"] == {"type": "adaptive"}
    assert body["max_tokens"] == 4096  # adaptive 无预算概念，不抬 max_tokens


def test_claude_4_6_uses_adaptive(transport):
    body = transport.build_request(_cfg("claude-sonnet-4-6"))
    assert body["thinking"] == {"type": "adaptive"}


def test_claude_major_only_5_uses_adaptive(transport):
    body = transport.build_request(_cfg("claude-opus-5"))
    assert body["thinking"] == {"type": "adaptive"}


def test_adaptive_drops_temperature(transport):
    body = transport.build_request(_cfg("claude-sonnet-5-5", **{"温度": 0.7}))
    assert "temperature" not in body


def test_effort_normalized_by_caps_values(transport, mod, monkeypatch):
    monkeypatch.setattr(
        mod,
        "resolve_model_capabilities",
        lambda cfg: {"thinking_enable_value": "adaptive", "reasoning_effort_values": ["low", "medium", "high"]},
    )
    body = transport.build_request(_cfg("claude-sonnet-5-5", **{"思考等级": "max"}))
    assert body["output_config"] == {"effort": "medium"}  # 非法档位回退三档中间值


def test_effort_invalid_without_caps_falls_back(transport):
    body = transport.build_request(_cfg("claude-sonnet-5-5", **{"思考等级": "minimal"}))
    assert body["output_config"] == {"effort": "high"}  # 协议全集的中间值


# ---------- 2. 老模型（4.5 及以下）：enabled + budget_tokens 不变 ----------


def test_claude_4_5_keeps_enabled_budget(transport):
    body = transport.build_request(_cfg("claude-opus-4-5-20251101", **{"思考预算": 4096}))
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    assert "output_config" not in body


def test_claude_dot_version_4_5_keeps_enabled(transport):
    body = transport.build_request(_cfg("claude-haiku-4.5"))
    assert body["thinking"]["type"] == "enabled"


def test_claude_4_0_date_suffix_not_parsed_as_minor(transport):
    # 日期后缀 20250514 不得被当成 minor=20 → 4.0 仍走 enabled
    body = transport.build_request(_cfg("claude-opus-4-20250514"))
    assert body["thinking"]["type"] == "enabled"


def test_claude_legacy_3_5_keeps_enabled(transport):
    body = transport.build_request(_cfg("claude-3-5-sonnet-20241022"))
    assert body["thinking"]["type"] == "enabled"


def test_enabled_bumps_max_tokens_above_budget(transport):
    body = transport.build_request(
        _cfg("claude-sonnet-4-5-20250929", **{"思考预算": 8192, "最大输出": 4096})
    )
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    assert body["max_tokens"] > 8192


# ---------- 3. caps 显式声明优先于版本启发式 ----------


def test_caps_enable_value_overrides_version(transport, mod, monkeypatch):
    monkeypatch.setattr(
        mod, "resolve_model_capabilities", lambda cfg: {"thinking_enable_value": "enabled"}
    )
    body = transport.build_request(_cfg("claude-sonnet-5-5"))
    assert body["thinking"]["type"] == "enabled"


def test_caps_adaptive_for_unknown_alias(transport, mod, monkeypatch):
    monkeypatch.setattr(
        mod, "resolve_model_capabilities", lambda cfg: {"thinking_enable_value": "adaptive"}
    )
    body = transport.build_request(_cfg("my-relay-claude-alias", **{"思考等级": "low"}))
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "low"}


def test_unknown_model_defaults_enabled(transport):
    body = transport.build_request(_cfg("some-relay-alias"))
    assert body["thinking"]["type"] == "enabled"


# ---------- 4. 关闭思考：行为不变（不发 thinking 字段） ----------


def test_thinking_off_sends_nothing(transport):
    body = transport.build_request({"模型名称": "claude-sonnet-5-5", "思考模式": False})
    assert "thinking" not in body
    assert "output_config" not in body
# ---------- 5. 采样参数分代：4.7+ 弃用（非默认值即 400），4.6 及以下照常透传 ----------

def test_claude_4_7_drops_sampling_params(transport):
    cfg = {'模型名称': 'claude-opus-4-7', '思考模式': False,
           '温度': 0.7, 'top_p': 0.9, 'top_k': 40}
    body = transport.build_request(cfg)
    assert 'temperature' not in body
    assert 'top_p' not in body
    assert 'top_k' not in body

def test_claude_5_x_drops_sampling_params_with_thinking(transport):
    cfg = {'模型名称': 'claude-sonnet-5', '思考模式': True, '温度': 0.7, 'top_p': 0.9}
    body = transport.build_request(cfg)
    assert 'temperature' not in body
    assert 'top_p' not in body

def test_claude_4_6_still_passes_sampling_params(transport):
    cfg = {'模型名称': 'claude-sonnet-4-6', '思考模式': False,
           '温度': 0.7, 'top_p': 0.9, 'top_k': 40}
    body = transport.build_request(cfg)
    assert body['temperature'] == 0.7
    assert body['top_p'] == 0.9
    assert body['top_k'] == 40

def test_claude_4_0_date_suffix_keeps_sampling_params(transport):
    # 日期后缀不构成 minor=20：4.0 不在弃用代内
    cfg = {'模型名称': 'claude-opus-4-20250514', '思考模式': False, 'top_p': 0.9}
    body = transport.build_request(cfg)
    assert body['top_p'] == 0.9

def test_claude_legacy_3_5_keeps_sampling_params(transport):
    cfg = {'模型名称': 'claude-3-5-sonnet-20241022', '思考模式': False, 'top_p': 0.9}
    body = transport.build_request(cfg)
    assert body['top_p'] == 0.9
