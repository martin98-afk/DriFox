# -*- coding: utf-8 -*-
"""P1-9 批 a：resolve_model_capabilities 四层链回归。

覆盖：
1. 六键声明全量生效（L0 压制所有层）
2. 空声明走自动链（空串/None = 自动）
3. 层序 caps > family > default（注入桩隔离外部数据源）
4. 声明值解析失败跳层（不抛错）
5. 思考强度/思考参数的宽松解析
6. sources 输出参数就地填充
7. 中文声明键被 _VALID_IDENTIFIER_PATTERN + PARAM_SCHEMA 双挡（两个挡点原文锁死）
"""

import re
import importlib.util
from pathlib import Path

import pytest

import app.core.modelmeta.model_capabilities as mc
from app.constants import PARAM_SCHEMA
from app.core.modelmeta.model_capabilities import (
    get_model_capability_sources,
    resolve_model_capabilities,
)

_UNKNOWN_MODEL = "decl-test-unknown-model"


def _load_transport_module():
    """插件目录非包，按路径 importlib 加载（对齐 tests/plugins 先例）"""
    module_path = (
        Path(__file__).resolve().parent.parent.parent
        / "plugins"
        / "system-transports"
        / "transports"
        / "openai_chat.py"
    )
    spec = importlib.util.spec_from_file_location("openai_chat_transport_decls_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def no_family(monkeypatch):
    """L3 family 桩：返回空 profile，隔离 ProviderRegistry 真实加载"""
    monkeypatch.setattr(mc, "get_provider_profile", lambda cfg: {}, raising=True)


def _caps_stub(mapping):
    return lambda model, provider="": dict(mapping)


# ── L0 声明层 ──────────────────────────────────────────────


def test_declared_overrides_all_layers(no_family):
    """六键声明全给 → 全部取声明值，sources 全标 declared"""
    llm_config = {
        "模型名称": _UNKNOWN_MODEL,
        "声明_上下文长度": 200000,
        "声明_最大输出": 8192,
        "声明_支持图像": True,
        "声明_支持思考": True,
        "声明_思考参数": "thinking_budget",
        "声明_思考强度": "low, medium",
    }
    sources: dict = {}
    out = resolve_model_capabilities(llm_config, "某服务商", sources)

    assert out["context_limit"] == 200000
    assert out["max_output_tokens"] == 8192
    assert out["supports_vision"] is True
    assert out["supports_thinking"] is True
    assert out["thinking_param"] == "thinking_budget"
    assert out["reasoning_effort_values"] == ["low", "medium"]
    assert set(sources.values()) == {"declared"}, f"六键应全标 declared，实际 {sources}"


def test_empty_declaration_falls_through(no_family):
    """声明空串/None = 自动 → 走自动链，无 declared 层"""
    llm_config = {
        "模型名称": _UNKNOWN_MODEL,
        "声明_上下文长度": "",
        "声明_支持图像": None,
        "声明_支持思考": "",
    }
    sources: dict = {}
    out = resolve_model_capabilities(llm_config, "", sources)

    assert "declared" not in set(sources.values()), "空声明不得标 declared"
    # 未知模型 + 空 family → caps 全 miss → context_limit 落 default 兜底
    assert out["context_limit"] == 128000
    assert sources["context_limit"] == "default"
    # supports_*/thinking_param 全链无值 → 不出现在 out
    assert "supports_vision" not in out
    assert "supports_thinking" not in out
    assert sources["supports_vision"] is None


# ── 层序：caps > family > default ──────────────────────────


def test_caps_layer_beats_family_and_default(monkeypatch, no_family):
    """caps 命中的键不再走 family/default"""
    monkeypatch.setattr(
        mc,
        "get_model_capabilities",
        _caps_stub(
            {
                "context_limit": 262144,
                "supports_vision": True,
                "supports_thinking": True,
                "thinking_param": "thinking",
                "reasoning_effort_values": ["high", "max"],
            }
        ),
    )
    sources: dict = {}
    out = resolve_model_capabilities({"模型名称": _UNKNOWN_MODEL}, "", sources)

    assert out["context_limit"] == 262144
    assert sources["context_limit"] == "caps"
    assert out["supports_vision"] is True
    assert sources["supports_vision"] == "caps"
    assert sources["max_output_tokens"] is None, "max_output_tokens 无兜底（语义拆分后不设 default）"


def test_family_layer_used_when_caps_misses(monkeypatch):
    """caps 缺键 → 落 family 声明"""
    monkeypatch.setattr(mc, "get_model_capabilities", _caps_stub({"supports_thinking": True}))
    monkeypatch.setattr(
        mc,
        "get_provider_profile",
        lambda cfg: {"thinking_param": "reasoning_effort", "max_output_tokens": 16384},
    )
    sources: dict = {}
    out = resolve_model_capabilities({"模型名称": _UNKNOWN_MODEL}, "", sources)

    assert out["thinking_param"] == "reasoning_effort"
    assert sources["thinking_param"] == "family"
    assert out["max_output_tokens"] == 16384
    assert sources["max_output_tokens"] == "family"
    assert out["supports_thinking"] is True
    assert sources["supports_thinking"] == "caps"


def test_invalid_declaration_skips_layer(monkeypatch, no_family):
    """声明值解析失败（非数字上下文）→ 跳过 declared 走自动链，不抛错"""
    monkeypatch.setattr(mc, "get_model_capabilities", _caps_stub({"context_limit": 4096}))
    sources: dict = {}
    out = resolve_model_capabilities(
        {"模型名称": _UNKNOWN_MODEL, "声明_上下文长度": "abc", "声明_支持思考": "yes"}, "", sources
    )

    assert out["context_limit"] == 4096, "非法声明应跳层取 caps"
    assert sources["context_limit"] == "caps"
    assert out["supports_thinking"] is True
    assert sources["supports_thinking"] == "declared", "合法的字符串声明应解析成功"


def test_declared_false_is_explicit_not_empty(monkeypatch):
    """声明_支持图像=False 是显式关闭（非空值）→ 压制 caps 的 True"""
    monkeypatch.setattr(mc, "get_model_capabilities", _caps_stub({"supports_vision": True}))
    out = resolve_model_capabilities({"模型名称": _UNKNOWN_MODEL, "声明_支持图像": False}, "")

    assert out["supports_vision"] is False, "显式 False 不得被 caps 的 True 反超"


# ── 解析细节 ───────────────────────────────────────────────


def test_thinking_values_loose_parsing(no_family):
    """思考强度支持中英文分隔符混拆；思考参数去首尾空白"""
    out = resolve_model_capabilities(
        {
            "模型名称": _UNKNOWN_MODEL,
            "声明_思考强度": "high、medium/low;max",
            "声明_思考参数": "  reasoning_effort ",
        }
    )
    assert out["reasoning_effort_values"] == ["high", "medium", "low", "max"]
    assert out["thinking_param"] == "reasoning_effort"


def test_sources_dict_filled_in_place(no_family):
    """sources 输出参数：调用方 dict 被就地填充且同对象返回引用内容"""
    llm_config = {"模型名称": _UNKNOWN_MODEL, "声明_最大输出": 4096}
    sources: dict = {}
    resolve_model_capabilities(llm_config, "", sources)
    assert sources["max_output_tokens"] == "declared"
    assert set(sources) == {
        "context_limit",
        "max_output_tokens",
        "supports_vision",
        "supports_thinking",
        "thinking_param",
        "reasoning_effort_values",
    }


def test_get_capability_sources_auto_chain_only(no_family):
    """get_model_capability_sources：无声明静态链，永远不含 declared"""
    sources = get_model_capability_sources(_UNKNOWN_MODEL)
    assert "declared" not in set(sources.values())
    assert sources["context_limit"] == "default"


# ── 批 b：apply_model_defaults declared 参数（effective 时序） ──


@pytest.fixture()
def stub_caps(monkeypatch):
    """apply_model_defaults 的 caps 桩：注入确定能力，隔离 models.dev"""
    holder: dict = {}

    def _install(caps: dict):
        holder.update(caps)
        monkeypatch.setattr(mc, "get_model_capabilities", lambda model, provider="": dict(caps))

    return _install


def test_apply_declared_true_keeps_thinking_fields(stub_caps):
    """红线（时序）：caps 说模型不支持思考，但声明_支持思考=True → 三键不摘

    复现真实调用时序：config 里已有用户保存的思考字段（稍后 overrides 才
    update 进来），apply 先跑——摘除会把用户配置误删，声明必须提前感知。
    """
    stub_caps({"context_limit": 32000, "supports_thinking": False})
    config = {"思考模式": True, "思考等级": "high", "思考预算": 4096}
    out = mc.apply_model_defaults(config, _UNKNOWN_MODEL, declared={"声明_支持思考": True})

    assert out["思考模式"] is True
    assert out["思考等级"] == "high"
    assert out["思考预算"] == 4096


def test_apply_declared_false_skips_fill(stub_caps):
    """声明_支持思考=False：caps 说支持也不补默认（不替用户做主）"""
    stub_caps(
        {
            "context_limit": 32000,
            "supports_thinking": True,
            "thinking_param": "reasoning_effort",
            "reasoning_effort_values": ["low", "high"],
        }
    )
    out = mc.apply_model_defaults({}, _UNKNOWN_MODEL, declared={"声明_支持思考": False})

    assert "思考模式" not in out, "声明 False 时不得补思考模式默认"
    assert "思考等级" not in out, "声明 False 时不得补思考等级默认"


def test_apply_no_declared_original_behavior(stub_caps):
    """无声明：原行为分毫不差——caps 不支持摘三键，caps 支持补默认"""
    stub_caps({"context_limit": 32000, "supports_thinking": False})
    out = mc.apply_model_defaults({"思考模式": True, "思考等级": "high"}, _UNKNOWN_MODEL)
    assert "思考模式" not in out and "思考等级" not in out and "思考预算" not in out

    stub_caps(
        {
            "context_limit": 32000,
            "supports_thinking": True,
            "thinking_param": "reasoning_effort",
            "reasoning_effort_values": ["low", "medium", "high"],
        }
    )
    out = mc.apply_model_defaults({}, _UNKNOWN_MODEL)
    assert out["思考模式"] is True
    assert out["思考等级"] == "low"  # values 首个非关闭档位
    assert out["最大Token"] == 32000


def test_ensure_thinking_fields_effective(monkeypatch, no_family):
    """_ensure_thinking_fields effective 判定：声明非空不摘；无声明摘且 caps 带 provider"""
    from app.main_widget import OpenAIChatToolWindow

    calls: list = []

    def _fake_caps(model, provider=""):
        calls.append((model, provider))
        return {"supports_thinking": False}

    monkeypatch.setattr(mc, "get_model_capabilities", _fake_caps)

    inst = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    inst._current_model_name = "m1"
    inst._current_provider_name = "prov"
    inst._valid_configs = {"prov": {"provider_name": "real-prov"}}

    # ① 声明非空 → 以声明为准，不摘不查 caps
    config = {"思考模式": True, "声明_支持思考": True}
    inst._ensure_thinking_fields(config)
    assert config["思考模式"] is True
    assert calls == []

    # ② 声明空串 = 自动 → 查 caps 且 provider 传 real-prov（分区精确查）
    config = {"思考模式": True, "思考等级": "high", "声明_支持思考": ""}
    inst._ensure_thinking_fields(config)
    assert calls == [("m1", "real-prov")], f"caps 查询应带 provider，实际 {calls}"
    assert "思考模式" not in config and "思考等级" not in config


# ── 双挡防护：中文声明键不进请求体 ──────────────────────────


def test_openai_chat_transport_drops_declaration_keys():
    """transport build_request_kwargs：声明键不进 extra_body/top_level

    第一挡：PARAM_SCHEMA 无 api_param；第二挡：中文键不匹配
    _VALID_IDENTIFIER_PATTERN。两挡任一失效本例即红。
    """
    module = _load_transport_module()
    transport = module.OpenAIChatTransport()
    llm_config = {
        "模型名称": "m1",
        "provider_name": "p1",
        "API_KEY": "sk-x",
        "API_URL": "https://api.example.com",
        "温度": 0.7,
        "声明_上下文长度": 200000,
        "声明_最大输出": 8192,
        "声明_支持图像": True,
        "声明_支持思考": True,
        "声明_思考参数": "thinking",
        "声明_思考强度": "high",
    }
    result = transport.build_request_kwargs(llm_config)
    extra_body = result["extra_body"]
    top_level = result["top_level"]

    def _has_chinese(key: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in key)

    assert not any(_has_chinese(k) for k in extra_body), f"中文声明键泄漏 extra_body: {list(extra_body)}"
    assert not any(_has_chinese(k) for k in top_level), f"中文声明键泄漏 top_level: {list(top_level)}"
    assert "temperature" in top_level, "常规参数不受影响"
    assert "max_tokens" not in extra_body, "声明_最大输出不是「最大输出」键，不得触发 max_tokens"


def test_subagent_worker_pattern_blocks_chinese():
    """subagent_worker 同款第二挡：中文键正则不匹配（与 openai_chat 同一 pattern 源）"""
    from app.core.workers.subagent_worker import _VALID_IDENTIFIER_PATTERN as sub_pattern

    transport_pattern = _load_transport_module()._VALID_IDENTIFIER_PATTERN

    for decl_key in (
        "声明_上下文长度",
        "声明_最大输出",
        "声明_支持图像",
        "声明_支持思考",
        "声明_思考参数",
        "声明_思考强度",
    ):
        assert not sub_pattern.match(decl_key), f"{decl_key} 不应匹配标识符正则（第二挡失效）"
        assert not transport_pattern.match(decl_key)
        assert PARAM_SCHEMA.get(decl_key, {}).get("api_param") is None, f"{decl_key} 不得配 api_param（第一挡失效）"


# ── 批 d：声明值经 resolve 链影响发送侧 ────────────────────


def test_apply_thinking_declared_param_overrides_caps():
    """声明_思考参数压制 caps：caps 说 reasoning_effort，声明 thinking_budget → 走 budget 分支"""
    module = _load_transport_module()
    llm_config = {
        "模型名称": _UNKNOWN_MODEL,
        "provider_name": "p1",
        "思考模式": True,
        "思考预算": 8192,
        "声明_思考参数": "thinking_budget",
    }
    extra_body: dict = {}
    module.OpenAIChatTransport._apply_thinking(extra_body, llm_config, _UNKNOWN_MODEL)

    assert extra_body.get("thinking_budget") == 8192, "声明参数应生效"
    assert "reasoning_effort" not in extra_body and "thinking" not in extra_body


def test_apply_thinking_declared_effort_values(monkeypatch):
    """声明_思考强度注入 values：保存等级在声明值内保留，不在则回退"""
    module = _load_transport_module()
    monkeypatch.setattr(mc, "get_model_capabilities", _caps_stub({}))

    base = {
        "模型名称": _UNKNOWN_MODEL,
        "provider_name": "p1",
        "思考模式": True,
        "声明_思考参数": "reasoning_effort",
        "声明_思考强度": "minimal, low, high",
    }

    extra_body: dict = {}
    cfg = {**base, "思考等级": "low"}
    module.OpenAIChatTransport._apply_thinking(extra_body, cfg, _UNKNOWN_MODEL)
    assert extra_body["reasoning_effort"] == "low", "声明值内的等级应保留"

    extra_body = {}
    cfg = {**base, "思考等级": "ultra"}
    module.OpenAIChatTransport._apply_thinking(extra_body, cfg, _UNKNOWN_MODEL)
    assert extra_body["reasoning_effort"] == "low", "无效等级应回退声明 values 的中位（低偏保守）"


def test_apply_thinking_enable_value_passthrough(monkeypatch):
    """thinking_enable_value 透传：MiniMax adaptive 不回退成 enabled"""
    module = _load_transport_module()
    monkeypatch.setattr(
        mc,
        "get_model_capabilities",
        _caps_stub({"supports_thinking": True, "thinking_param": "thinking", "thinking_enable_value": "adaptive"}),
    )
    llm_config = {"模型名称": "minimax-m2", "provider_name": "p1", "思考模式": True}
    extra_body: dict = {}
    module.OpenAIChatTransport._apply_thinking(extra_body, llm_config, "minimax-m2")

    assert extra_body.get("thinking") == {"type": "adaptive"}, "enable_value 应从 caps 透传"


def test_resolve_vision_declared_true_forces_visual(monkeypatch):
    """视觉判定同源：caps 无 vision 数据，声明_支持图像=True → supports_vision True"""
    monkeypatch.setattr(mc, "get_model_capabilities", _caps_stub({}))
    out = resolve_model_capabilities({"模型名称": _UNKNOWN_MODEL, "声明_支持图像": True})
    assert out["supports_vision"] is True


def test_responses_branch_effort_via_declared_values(monkeypatch):
    """responses 分支：effort 经声明 values normalize（_build_responses_kwargs 同源逻辑）"""
    from app.core.workers.chat_worker import normalize_reasoning_effort as norm

    monkeypatch.setattr(mc, "get_model_capabilities", _caps_stub({}))
    llm_config = {
        "模型名称": _UNKNOWN_MODEL,
        "声明_思考强度": "low, high",
        "声明_思考参数": "reasoning_effort",
    }
    values = resolve_model_capabilities(llm_config).get("reasoning_effort_values")

    assert norm("high", values) == "high"
    assert norm("ultra", values) == "low", "无效等级回退声明 values 中位"
