# -*- coding: utf-8 -*-
"""插件 per-model 能力声明（ProviderDef.model_capabilities）解析链回归。

覆盖：
1. 插件命中即整体返回（provider 维度最精确，压过 models.dev / 硬编码）
2. 未命中降级原链（models.dev / 硬编码行为不变）
3. 不传 provider → 不查插件层，既有调用行为不变
4. 贯通：resolve_max_output_tokens / get_caps_max_output_tokens 吃到插件值
"""

import pytest

import app.core.modelmeta.model_capabilities as mc
from app.core.modelmeta.model_capabilities import (
    get_caps_max_output_tokens,
    get_model_capabilities,
    resolve_max_output_tokens,
)
from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry


_PROVIDER = "PerModel Test Provider"


@pytest.fixture()
def provider_registry(monkeypatch):
    """独立 registry + 注册带 per-model 声明的 ProviderDef"""
    reg = ProviderRegistry()
    reg.register(
        ProviderDef(
            name=_PROVIDER,
            family="permodel_test",
            api_url="https://permodel.example/v1",
            models=["gw-opus"],
            capabilities={"context_limit": 8000},
            model_capabilities={
                "gw-opus": {
                    "context_limit": 400000,
                    "max_output_tokens": 131072,
                    "absolute_limit": 131072,
                    "supports_thinking": True,
                    "thinking_param": "reasoning_effort",
                    "reasoning_effort_values": ["low", "high"],
                    "supports_vision": True,
                }
            },
        )
    )
    monkeypatch.setattr(ProviderRegistry, "get_instance", staticmethod(lambda: reg))
    return reg


def test_provider_per_model_hit_wins(provider_registry, monkeypatch):
    """插件命中即整体返回，压过 models.dev / 硬编码同层竞争者"""
    # models.dev 动态返回不同值（桩在分层查询入口），插件声明必须赢
    monkeypatch.setattr(mc, "_get_partitioned_model_capabilities", lambda n, p: None)
    monkeypatch.setattr(mc, "_get_dynamic_model_capabilities", lambda n: None)
    caps = get_model_capabilities("gw-opus", _PROVIDER)
    assert caps["context_limit"] == 400000
    assert caps["max_output_tokens"] == 131072
    assert caps["thinking_param"] == "reasoning_effort"
    assert caps["reasoning_effort_values"] == ["low", "high"]


def test_provider_per_model_cost_filled_from_lower(provider_registry, monkeypatch):
    """插件未声明的展示性字段（cost）从 models.dev 补缺；限额不被反向覆盖

    回归（2026-10-13 用户实测）：命中插件层后价格列全 "-"——插件不声明
    cost，整体返回把 models.dev 的价格挡掉了。
    """
    monkeypatch.setattr(
        mc,
        "_get_partitioned_model_capabilities",
        lambda n, p: {
            "context_limit": 111111,  # 下层同键 → 被插件压住
            "max_output_tokens": 999,
            "cost": {"input": 0.1, "output": 0.5, "cache_read": 0.01, "cache_write": 0.12},
        },
    )
    monkeypatch.setattr(mc, "_get_dynamic_model_capabilities", lambda n: None)
    caps = get_model_capabilities("gw-opus", _PROVIDER)
    # 插件权威键不拉回
    assert caps["context_limit"] == 400000
    assert caps["max_output_tokens"] == 131072
    # 插件未声明的展示字段补缺
    assert caps["cost"] == {"input": 0.1, "output": 0.5, "cache_read": 0.01, "cache_write": 0.12}


def test_provider_per_model_beats_models_dev(provider_registry, monkeypatch):
    """models.dev 有数据时插件仍优先（网关限额与官方 API 各自成立）"""
    monkeypatch.setattr(
        mc,
        "_get_partitioned_model_capabilities",
        lambda n, p: {"context_limit": 111111, "max_output_tokens": 999},
    )
    caps = get_model_capabilities("gw-opus", _PROVIDER)
    assert caps["context_limit"] == 400000
    assert caps["max_output_tokens"] == 131072


def test_provider_per_model_miss_falls_through(provider_registry, monkeypatch):
    """provider 无该模型声明 → 降级 models.dev → 硬编码，行为不变"""
    monkeypatch.setattr(mc, "_get_partitioned_model_capabilities", lambda n, p: None)
    monkeypatch.setattr(mc, "_get_dynamic_model_capabilities", lambda n: None)
    caps = get_model_capabilities("unknown-model", _PROVIDER)
    assert caps == {}


def test_no_provider_arg_skips_provider_layer(provider_registry):
    """不传 provider（15 处既有调用）→ 不查插件层"""
    assert get_model_capabilities("gw-opus") == {}


def test_unregistered_provider_skips_layer(provider_registry):
    """provider 名未注册 → 空返回不抛"""
    assert get_model_capabilities("gw-opus", "Not Registered Provider") == {}


def test_pipeline_caps_max_output_tokens(provider_registry):
    """贯通：输出上限链 L0（caps）吃到插件 per-model 值"""
    llm_config = {"模型名称": "gw-opus", "provider_name": _PROVIDER}
    assert get_caps_max_output_tokens("gw-opus", llm_config) == 131072
    # 用户未显式填最大输出 → resolve 链取 caps 值而非 family 兜底
    assert resolve_max_output_tokens(llm_config, default=4096) == 131072


def test_pipeline_context_limit_via_caps(provider_registry, monkeypatch):
    """贯通：resolve_context_limit 链在 L2（caps）层吃到插件值"""
    from app.core.modelmeta.model_capabilities import resolve_context_limit

    monkeypatch.setattr(mc, "_get_partitioned_model_capabilities", lambda n, p: None)
    monkeypatch.setattr(mc, "_get_dynamic_model_capabilities", lambda n: None)
    llm_config = {"模型名称": "gw-opus", "provider_name": _PROVIDER}
    assert resolve_context_limit(llm_config) == 400000
