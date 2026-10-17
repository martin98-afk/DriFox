# -*- coding: utf-8 -*-
"""服务商 UI 元数据基座（app/utils/provider_ui_meta.py）单元测试（P1-18）

覆盖三个取值函数的「插件声明优先 → 回退」链，以及各类兜底：
- get_auth_type：插件声明 → 存档值 → bearer
- get_preset_urls：preset_urls → [api_url] → []，去重保序
- get_family：插件声明 → ""

隔离手段：monkeypatch ProviderRegistry 单例，不依赖真实插件扫描。
"""

import pytest

from app.plugins.registries.provider_registry import ProviderDef, ProviderRegistry


@pytest.fixture()
def registry(monkeypatch):
    """把单例换成隔离注册表；返回可注册 ProviderDef 的句柄"""
    reg = ProviderRegistry()
    monkeypatch.setattr(ProviderRegistry, "_instance", reg)
    monkeypatch.setattr(ProviderRegistry, "get_instance", classmethod(lambda cls: reg))
    return reg


# ══════════════════════════════════════════════════════════════════
# get_auth_type（4 例）
# ══════════════════════════════════════════════════════════════════


class TestGetAuthType:
    def test_declared_wins_over_saved(self, registry):
        """插件声明优先：百度千帆声明 bce，即使存档是 bearer 也用 bce

        这是 P0-5 修的核心缺陷——UI 曾把认证方式写死 bearer，
        覆盖插件声明的 bce 签名，导致百度千帆无法正确配置。
        """
        from app.utils.provider_ui_meta import get_auth_type

        registry.register(ProviderDef(name="百度千帆", auth_type="bce"), source="plugin:test")
        assert get_auth_type("百度千帆", "bearer") == "bce"

    def test_falls_back_to_saved_when_undeclared(self, registry):
        """未注册插件 → 用存档值（用户自定义服务商的认证方式得以保留）"""
        from app.utils.provider_ui_meta import get_auth_type

        assert get_auth_type("我的自建服务", "none") == "none"

    def test_defaults_to_bearer(self, registry):
        """未注册插件且存档为空 → 兜底 bearer"""
        from app.utils.provider_ui_meta import get_auth_type

        assert get_auth_type("我的自建服务") == "bearer"
        assert get_auth_type("我的自建服务", "") == "bearer"

    def test_blank_declaration_falls_through_to_saved(self, registry):
        """插件显式声明空 auth_type → 视为未声明，落到存档值"""
        from app.utils.provider_ui_meta import get_auth_type

        registry.register(ProviderDef(name="空声明服务商", auth_type=""), source="plugin:test")
        assert get_auth_type("空声明服务商", "anthropic") == "anthropic"


# ══════════════════════════════════════════════════════════════════
# get_preset_urls（4 例）
# ══════════════════════════════════════════════════════════════════


class TestGetPresetURLs:
    def test_returns_declared_urls(self, registry):
        """插件声明 preset_urls → 原样返回（阿里云双 URL）"""
        from app.utils.provider_ui_meta import get_preset_urls

        registry.register(
            ProviderDef(
                name="阿里云 (DashScope)",
                api_url="https://token-plan.example.com/v1",
                preset_urls=[
                    "https://token-plan.example.com/v1",
                    "https://llm-example.cn-beijing.maas.aliyuncs.com/v1",
                ],
            ),
            source="plugin:test",
        )
        assert get_preset_urls("阿里云 (DashScope)") == [
            "https://token-plan.example.com/v1",
            "https://llm-example.cn-beijing.maas.aliyuncs.com/v1",
        ]

    def test_falls_back_to_api_url(self, registry):
        """未声明 preset_urls → 用 api_url 单条兜底"""
        from app.utils.provider_ui_meta import get_preset_urls

        registry.register(ProviderDef(name="DeepSeek", api_url="https://api.deepseek.com"), source="plugin:test")
        assert get_preset_urls("DeepSeek") == ["https://api.deepseek.com"]

    def test_returns_empty_for_unknown_provider(self, registry):
        """未注册插件 → 空列表（不抛异常）"""
        from app.utils.provider_ui_meta import get_preset_urls

        assert get_preset_urls("不存在的服务商") == []

    def test_dedupes_and_keeps_order(self, registry):
        """preset_urls 有重复 → 去重且保持声明顺序"""
        from app.utils.provider_ui_meta import get_preset_urls

        registry.register(
            ProviderDef(
                name="重复声明服务商",
                api_url="https://a.example.com",
                preset_urls=["https://a.example.com", "https://b.example.com", "https://a.example.com"],
            ),
            source="plugin:test",
        )
        assert get_preset_urls("重复声明服务商") == ["https://a.example.com", "https://b.example.com"]

    def test_whitespace_only_entries_filtered_and_stripped(self, registry):
        """R2-2：声明里混入空白串 → strip 后滤空；有值项去除首尾空白（与 fallback 口径一致）"""
        from app.utils.provider_ui_meta import get_preset_urls

        registry.register(
            ProviderDef(
                name="脏声明服务商",
                api_url="https://a.example.com",
                preset_urls=["   ", "  https://b.example.com  ", "\t", "https://c.example.com"],
            ),
            source="plugin:test",
        )
        assert get_preset_urls("脏声明服务商") == ["https://b.example.com", "https://c.example.com"]

    def test_all_blank_entries_fall_back_to_api_url(self, registry):
        """声明全是空白串 → 视为未声明，回落 api_url 兜底"""
        from app.utils.provider_ui_meta import get_preset_urls

        registry.register(
            ProviderDef(name="全空白服务商", api_url="https://a.example.com", preset_urls=["", "   "]),
            source="plugin:test",
        )
        assert get_preset_urls("全空白服务商") == ["https://a.example.com"]


# ══════════════════════════════════════════════════════════════════
# get_family（3 例）
# ══════════════════════════════════════════════════════════════════


class TestGetFamily:
    def test_returns_declared_family(self, registry):
        """插件声明 family → 原样返回"""
        from app.utils.provider_ui_meta import get_family

        registry.register(ProviderDef(name="智谱AI", family="zhipu"), source="plugin:test")
        assert get_family("智谱AI") == "zhipu"

    def test_returns_empty_for_unknown_provider(self, registry):
        """未注册插件 → 空串（内置探测链留给调用方兜底）"""
        from app.utils.provider_ui_meta import get_family

        assert get_family("不存在的服务商") == ""

    def test_returns_empty_when_not_declared(self, registry):
        """插件未声明 family → 空串"""
        from app.utils.provider_ui_meta import get_family

        registry.register(ProviderDef(name="无族服务商"), source="plugin:test")
        assert get_family("无族服务商") == ""
