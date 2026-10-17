# -*- coding: utf-8 -*-
"""P1-17 回归：detect_provider_family 的「插件声明优先」层

背景
----
family 探测原先是 provider_profile.py 的 16 条 if 链（按 api_url / 模型名前缀
/ auth 判定），而 providers 插件早已声明 `ProviderDef.family`，两套并行 = 最大
双源漂移点。P1-17 在 if 链前置一层「声明命中直返」，未声明/未注册的服务商
落回原链（顺序与内容一字不动）。

本组锁定三层：
1. 声明命中 → 直返声明值（即使 api_url/模型名会诱使原链判成别的族）
2. 未声明 / 未注册 → 走原链（行为与改动前完全一致）
3. llm_config 缺 provider_name 键 → 走原链（老配置 / 测试替身形态）

配套：等价性比对已单独跑过 —— 12 家内置插件在「带 provider_name + 插件默认
api_url/default_model/auth」输入下，声明值与原链判定值**逐家一致**。
"""

import pytest

from app.core.modelmeta.provider_profile import detect_provider_family
from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry


@pytest.fixture()
def registry(monkeypatch):
    """隔离注册表（不依赖真实插件扫描）"""
    reg = ProviderRegistry()
    monkeypatch.setattr(ProviderRegistry, "_instance", reg)
    monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


# ══════════════════════════════════════════════════════════════════
# 1. 声明命中直返
# ══════════════════════════════════════════════════════════════════


class TestDeclaredWins:
    def test_declared_family_returned_verbatim(self, registry):
        """声明命中：直返声明值，不看 api_url / 模型名"""
        registry.register(ProviderDef(name="某服务商", family="my_family"), source="plugin:test")
        cfg = {
            "provider_name": "某服务商",
            "API_URL": "https://api.deepseek.com",  # 原链会判 deepseek
            "模型名称": "deepseek-chat",
        }
        assert detect_provider_family(cfg) == "my_family"

    def test_declared_overrides_model_prefix_trap(self, registry):
        """声明优先修复的正是「按模型名前缀误判」：SiliconFlow 默认模型是
        deepseek-ai/DeepSeek-R1，原链按模型名会判成 deepseek 族（把 SiliconFlow
        的能力参数错用成 DeepSeek 的）"""
        registry.register(
            ProviderDef(name="SiliconFlow (硅基流动)", family="siliconflow"),
            source="plugin:test",
        )
        cfg = {
            "provider_name": "SiliconFlow (硅基流动)",
            "API_URL": "https://api.siliconflow.cn/v1",
            "模型名称": "deepseek-ai/DeepSeek-R1",
        }
        assert detect_provider_family(cfg) == "siliconflow"

    def test_declared_wins_even_for_ollama_like_auth(self, registry):
        """声明优先于 auth 判定：「认证方式 none」原链会判 ollama，声明应赢"""
        registry.register(ProviderDef(name="自建网关", family="custom_gateway"), source="plugin:test")
        cfg = {"provider_name": "自建网关", "API_URL": "http://localhost:11434/v1", "认证方式": "none"}
        assert detect_provider_family(cfg) == "custom_gateway"


# ══════════════════════════════════════════════════════════════════
# 2. 未声明 / 未注册 → 原链
# ══════════════════════════════════════════════════════════════════


class TestFallsBackToChain:
    def test_unregistered_provider_walks_chain(self, registry):
        """未注册的服务商：注册表查不到 → 走原链（按 api_url 判定）"""
        assert detect_provider_family({"provider_name": "不存在", "API_URL": "https://api.openai.com/v1"}) == "openai"
        assert (
            detect_provider_family({"provider_name": "不存在", "API_URL": "https://api.deepseek.com"}) == "deepseek"
        )

    def test_registered_but_no_family_declared(self, registry):
        """已注册但未声明 family（family=""）→ 走原链"""
        registry.register(ProviderDef(name="无族服务商", api_url="https://x/v1", family=""), source="plugin:test")
        cfg = {"provider_name": "无族服务商", "API_URL": "https://bigmodel.cn/api/paas/v4"}
        assert detect_provider_family(cfg) == "zhipu"

    def test_chain_semantics_unchanged(self, registry):
        """原链判定语义不变：各分支抽样（顺序敏感项也在内）"""
        cases = [
            ({"API_URL": "https://copilot.tencent.com/v1"}, "codebuddy"),
            ({"API_URL": "https://x/v1", "模型名称": "claude-3-5-sonnet"}, "anthropic"),
            ({"API_URL": "https://generativelanguage.googleapis.com/v1"}, "gemini"),
            ({"API_URL": "https://dashscope.aliyuncs.com/v1"}, "dashscope"),
            ({"API_URL": "https://bigmodel.cn/v4"}, "zhipu"),
            ({"API_URL": "https://opencode.ai/zen/v1"}, "opencode"),
            ({"API_URL": "https://api.groq.com/openai/v1"}, "groq"),
            ({"API_URL": "https://api.siliconflow.cn/v1"}, "siliconflow"),
            ({"API_URL": "https://api.minimax.chat/v1"}, "minimax"),
            ({"API_URL": "https://ark.cn-beijing.volces.com/api/v3"}, "volcengine"),
            ({"API_URL": "https://qianfan.baidubce.com/v2"}, "baidu_qianfan"),
            ({"API_URL": "http://localhost:11434/v1"}, "ollama"),
            ({"API_URL": "http://localhost:1234/v1"}, "lmstudio"),
            ({"API_URL": "https://api.openai.com/v1"}, "openai"),
            ({"API_URL": "https://未知/v1"}, "custom"),
        ]
        for cfg, expect in cases:
            assert detect_provider_family(cfg) == expect, f"{cfg} 应判 {expect}"

    def test_bce_auth_maps_to_baidu(self, registry):
        """auth 判定分支：bce → baidu_qianfan（百度千帆的签名方式专属）"""
        assert detect_provider_family({"API_URL": "https://x/v1", "认证方式": "bce"}) == "baidu_qianfan"


# ══════════════════════════════════════════════════════════════════
# 3. 缺 provider_name 键
# ══════════════════════════════════════════════════════════════════


class TestMissingProviderNameKey:
    def test_absent_key_walks_chain(self, registry):
        """llm_config 无 provider_name 键（老配置 / 测试替身）→ 走原链，不抛异常"""
        assert detect_provider_family({"API_URL": "https://api.deepseek.com"}) == "deepseek"

    def test_empty_provider_name_walks_chain(self, registry):
        """provider_name 为空串 → 走原链（空名不查注册表）"""
        assert detect_provider_family({"provider_name": "", "API_URL": "https://bigmodel.cn/v4"}) == "zhipu"

    def test_none_provider_name_walks_chain(self, registry):
        """provider_name 为 None → 走原链（不把 None 传给注册表）"""
        assert detect_provider_family({"provider_name": None, "API_URL": "https://api.openai.com/v1"}) == "openai"
