# -*- coding: utf-8 -*-
"""服务商注册表（ProviderRegistry）单元测试

覆盖：
- 注册 / 同名保护 / source 清理
- 聚合视图（models / icon / default_config / quota / models_dev / family caps）
- 余额与套餐用量 fetcher 注册与执行
- providers 插件加载（system 14 家）
"""

from app.plugins.loaders.provider_loader import load_providers
from app.plugins.registries.provider_registry import (
    ProviderDef,
    ProviderRegistry,
    QuotaField,
    make_bearer_balance_fetcher,
)


def _fresh_registry():
    """每次测试用全新注册表（不污染单例）"""
    return ProviderRegistry()


def test_register_and_get():
    reg = _fresh_registry()
    p = ProviderDef(name="测试服务商", icon="test-icon", api_url="https://test.com/v1")
    assert reg.register(p, source="plugin:test") is True
    assert reg.get("测试服务商") is p
    assert reg.names() == ["测试服务商"]


def test_duplicate_name_rejected():
    reg = _fresh_registry()
    reg.register(ProviderDef(name="A"), source="plugin:x")
    # 同名重复注册被拒绝（先注册者优先）
    assert reg.register(ProviderDef(name="A"), source="plugin:y") is False
    assert reg.get("A").source == "plugin:x"


def test_clear_source():
    reg = _fresh_registry()
    reg.register(ProviderDef(name="A"), source="plugin:x")
    reg.register(ProviderDef(name="B"), source="plugin:y")
    reg.clear_source("plugin:x")
    assert reg.get("A") is None
    assert reg.get("B") is not None


def test_aggregation_views():
    reg = _fresh_registry()
    reg.register(
        ProviderDef(
            name="DeepSeek",
            icon="deepseek",
            api_url="https://api.deepseek.com",
            default_model="deepseek-chat",
            default_params={"温度": 0.7, "最大Token": 200000},
            register_url="https://platform.deepseek.com/api_keys",
            models=["deepseek-v4-flash", "deepseek-v4-pro"],
            models_dev_id="deepseek",
            family="deepseek",
            capabilities={"context_limit": 320000, "supports_thinking": True},
            extra_quota_fields=[QuotaField(key="cookie", label="Cookie:")],
        ),
        source="plugin:system",
    )

    assert reg.provider_models() == {"DeepSeek": ["deepseek-v4-flash", "deepseek-v4-pro"]}
    assert reg.icon_map() == {"DeepSeek": "deepseek"}
    cfg = reg.default_config("DeepSeek")
    assert cfg["API_URL"] == "https://api.deepseek.com"
    assert cfg["模型名称"] == "deepseek-chat"
    assert cfg["认证方式"] == "bearer"
    assert cfg["温度"] == 0.7
    assert cfg["获取地址"] == "https://platform.deepseek.com/api_keys"
    assert reg.default_config("不存在") is None
    assert reg.quota_exclude_keys() == frozenset({"cookie"})
    assert reg.models_dev_map() == {"DeepSeek": "deepseek"}
    caps = reg.family_capabilities("deepseek")
    assert caps["context_limit"] == 320000
    assert caps["supports_thinking"] is True
    # 未声明 family 的能力 → 回退 custom 兜底
    fallback = reg.family_capabilities("unknown-family")
    assert fallback["context_limit"] == 200000


def test_balance_fetcher_makes_bearer_request(monkeypatch):
    reg = _fresh_registry()
    reg.register(
        ProviderDef(
            name="DeepSeek",
            balance_fetcher=make_bearer_balance_fetcher(
                url="https://api.deepseek.com/user/balance",
                balance_key="total_balance",
                currency="¥",
            ),
        ),
        source="plugin:system",
    )
    assert reg.has_balance_support("DeepSeek") is True
    assert reg.has_balance_support("其他") is False

    captured = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"balance_infos": [{"total_balance": "1.23"}]}

    def _fake_get(url, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResp()

    import requests

    monkeypatch.setattr(requests, "get", _fake_get)

    result = reg.balance_fetch("DeepSeek", {"API_KEY": "sk-test"})
    assert result == {"balance": 1.23, "currency": "¥"}
    assert captured["url"] == "https://api.deepseek.com/user/balance"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"


def test_balance_fetcher_hide_without_key():
    reg = _fresh_registry()
    reg.register(
        ProviderDef(
            name="DeepSeek",
            balance_fetcher=make_bearer_balance_fetcher(
                url="https://api.deepseek.com/user/balance",
                balance_key="total_balance",
            ),
        ),
        source="plugin:system",
    )
    # 无 API key → 返回 None（不发起请求）
    assert reg.balance_fetch("DeepSeek", {}) is None


def test_coding_plan_fetcher_registration_and_resolution():
    reg = _fresh_registry()

    def _fake_plan(config):
        return {"rolling": {"percent": 50, "reset_sec": 100}}

    reg.register(
        ProviderDef(
            name="OpenCode Zen",
            family="opencode",
            coding_plan_fetcher=_fake_plan,
        ),
        source="plugin:system",
    )
    assert reg.has_coding_plan_support("OpenCode Zen") is True
    assert reg.has_coding_plan_support("不存在") is False
    result = reg.coding_plan_fetch("OpenCode Zen", {})
    assert result["rolling"]["percent"] == 50
    # 无获取器 → None
    assert reg.coding_plan_fetch("不存在", {}) is None


def test_system_providers_loaded_from_plugins():
    """system 插件 14 家服务商全部加载，关键数据完整"""
    reg = _fresh_registry()
    loaded = load_providers(registry=reg)
    assert "system" in loaded
    names = reg.names()
    assert len(names) == 14

    for expect in [
        "DeepSeek",
        "SiliconFlow (硅基流动)",
        "MiniMax",
        "智谱AI",
        "OpenAI",
        "Anthropic (Claude)",
        "Google Gemini",
        "Groq",
        "Ollama",
        "百度千帆",
        "阿里云 (DashScope)",
        "火山方舟",
        "OpenCode Zen",
        "OpenCode Go",
    ]:
        assert expect in names, f"缺少服务商 {expect}"

    # DeepSeek：icon + 默认 config + 余额 + family 能力
    deepseek = reg.get("DeepSeek")
    assert deepseek.icon == "deepseek"
    assert deepseek.default_model == "deepseek-chat"
    assert reg.has_balance_support("DeepSeek") is True
    assert reg.family_capabilities("deepseek")["context_limit"] == 320000
    # SiliconFlow 余额
    assert reg.has_balance_support("SiliconFlow (硅基流动)") is True
    # 用量 fetcher：火山方舟 / MiniMax / 智谱AI / OpenAI / OpenCode
    for name in ["火山方舟", "MiniMax", "智谱AI", "OpenAI", "OpenCode Zen", "OpenCode Go"]:
        assert reg.has_coding_plan_support(name), f"{name} 应有套餐用量 fetcher"
    # 用量额外字段：5 个 key 全由插件聚合
    quota_keys = reg.quota_exclude_keys()
    assert {"server_id", "cookie", "workspace_id", "csrf_token", "x_web_id"} <= quota_keys
    # models.dev 白名单
    dev_map = reg.models_dev_map()
    assert dev_map["DeepSeek"] == "deepseek"
    assert dev_map["OpenCode Go"] == "opencode-go"


def test_provider_icon_dirs_injected_from_plugin():
    """插件图标目录由 loader 自动注入（icons/ 深色 + icons_light/ 浅色）"""
    reg = _fresh_registry()
    load_providers(registry=reg)

    from pathlib import Path

    deepseek = reg.get("DeepSeek")
    # 深色图标目录指向系统插件 icons/
    assert deepseek.icon_dir
    assert Path(deepseek.icon_dir).name == "icons"
    assert (Path(deepseek.icon_dir) / f"{deepseek.icon}.svg").exists()
    # Groq 有浅色版本（icons_light/groq.svg 原样保留）
    groq = reg.get("Groq")
    assert groq.icon_dir_light
    assert (Path(groq.icon_dir_light) / f"{groq.icon}.svg").exists()
    # DeepSeek 无浅色版：icons_light 下不存在 deepseek.svg（深浅回退到深色图标）
    assert not (Path(deepseek.icon_dir_light) / f"{deepseek.icon}.svg").exists()


def test_provider_icon_light_missing_falls_back_to_dark():
    """浅色图标缺失时回退深色图标（icons 有、icons_light 无 → 深浅同一份）"""
    from app.utils.provider_icons import _provider_icon_path

    reg = _fresh_registry()
    load_providers(registry=reg)
    # 手动只为 DeepSeek 关闭浅色目录（模拟「无浅色版」场景）
    deepseek = reg.get("DeepSeek")
    assert "deepseek.svg" in _provider_icon_path("DeepSeek")