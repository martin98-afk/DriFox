# -*- coding: utf-8 -*-
"""OpenCode Zen (OAuth) 服务商插件：注册隔离 + device flow + 动态头 + 模型池。

覆盖 2026-11-07 落地的 console OAuth 接入（保留原 API Key 版 Zen/Go 不动）。
全部用 monkeypatch 打桩网络与磁盘缓存，不发真实请求、不写真实 app.config。
"""

import importlib.util
import time
from pathlib import Path

import pytest

_PLUGIN_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "system-providers"
    / "providers"
    / "opencode.py"
)


def _load_plugin():
    spec = importlib.util.spec_from_file_location("_test_opencode_plugin", _PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Registry:
    """最小注册表桩：只收集 ProviderDef.name / family / capabilities。"""

    def __init__(self):
        self.items = {}

    def register(self, provider, source=""):
        self.items[provider.name] = provider


@pytest.fixture()
def plugin(tmp_path):
    mod = _load_plugin()
    # 缓存目录重定向到已存在的 tmp：_cache_dir() 见 _CACHE_DIR 非 None 直接返回，
    # 不会 mkdir 也不会写真实 app_data_dir（每次用例重新 exec，模块状态天然隔离）
    mod._load_oauth()._CACHE_DIR = tmp_path
    return mod


# ── 注册与隔离 ────────────────────────────────────────────────

def test_four_providers_registered_api_key_versions_untouched(plugin):
    reg = _Registry()
    plugin.register(reg)
    assert set(reg.items) == {
        "OpenCode Zen", "OpenCode Go", "OpenCode Zen (OAuth)", "OpenCode Go (OAuth)",
    }
    zen, go = reg.items["OpenCode Zen"], reg.items["OpenCode Go"]
    assert zen.family == "opencode" and go.family == "opencode"
    assert zen.api_url == "https://opencode.ai/zen/v1"
    assert go.api_url == "https://opencode.ai/zen/go/v1"
    assert "login_hook" not in zen.capabilities
    assert "extra_headers" not in zen.capabilities


def test_oauth_uses_independent_family_and_group(plugin, monkeypatch):
    """独立 family 防串味；不声明 list_group 才会落到 OAuth 自动判据。"""
    from app.widgets.cards.settings.provider_setting_card import _group_of
    from app.plugins.registries.provider_registry import ProviderRegistry

    reg = _Registry()
    plugin.register(reg)
    zen_oauth = reg.items["OpenCode Zen (OAuth)"]
    go_oauth = reg.items["OpenCode Go (OAuth)"]
    assert zen_oauth.family == "opencode_zen_oauth"
    assert go_oauth.family == "opencode_go_oauth"
    assert zen_oauth.api_url == "https://opencode.ai/inference/openai/v1"
    assert go_oauth.api_url == "https://opencode.ai/inference/go/openai/v1"
    for p in (zen_oauth, go_oauth):
        caps = p.capabilities
        assert callable(caps["login_hook"]) and callable(caps["models_hook"])
        assert caps["session_header"] == "x-opencode-session"
        assert "list_group" not in caps  # 显式写 "API" 会压掉 OAuth 自动判据

    # 真实走 _group_of：两个 OAuth 版归 OAuth 组，API Key 版按声明分组
    live = ProviderRegistry()
    plugin.register(live)
    monkeypatch.setattr(ProviderRegistry, "get_instance", staticmethod(lambda: live))
    assert _group_of("OpenCode Zen (OAuth)", {}) == "OAuth"
    assert _group_of("OpenCode Go (OAuth)", {}) == "OAuth"
    assert _group_of("OpenCode Zen", {}) == "API"
    assert _group_of("OpenCode Go", {}) == "Coding Plan"


# ── 动态头 ────────────────────────────────────────────────────

def test_dynamic_headers_from_cache(plugin):
    oa = plugin._load_oauth()
    oa.cache_put(
        "rt-1",
        access_token="at-1",
        refresh_token="rt-1",
        expires_at=time.time() + 3600,
        org_id="org-9",
    )
    cfg = {"API_KEY": "rt-1", "模型名称": "glm-5.3-flash"}
    headers = plugin._OAUTH_EXTRA_HEADERS
    assert headers["Authorization"](cfg) == "Bearer at-1"
    assert headers["x-opencode-org-id"](cfg) == "org-9"
    assert headers["User-Agent"] == "opencode/cli"


def test_authorization_failure_does_not_throw(plugin):
    """callable 抛异常会被 resolve_extra_headers 吞掉整组头，故必须内部消化。"""
    cfg = {"API_KEY": "rt-bad"}
    assert plugin._OAUTH_EXTRA_HEADERS["Authorization"](cfg) == ""
    assert plugin._OAUTH_EXTRA_HEADERS["x-opencode-org-id"](cfg) == ""


def test_refresh_rotates_access_token_and_stable_cache_key(plugin, monkeypatch):
    oa = plugin._load_oauth()
    oa.cache_put(
        "rt-2",
        access_token="stale",
        refresh_token="rt-2",
        expires_at=time.time() - 1,
        org_id="org-2",
        allowed_models=["glm-5.3-flash"],
    )
    calls = []

    def _fake_device_token(body, timeout=None):
        calls.append(body)
        return {"access_token": "fresh", "refresh_token": "rt-2-rotated", "expires_in": 3600}

    monkeypatch.setattr(oa, "device_token", _fake_device_token)
    oa._REFRESH_LOCK = __import__("threading").Lock()

    access, org = oa.get_auth_context({"API_KEY": "rt-2"})
    assert (access, org) == ("fresh", "org-2")
    assert calls[0]["grant_type"] == "refresh_token"
    assert calls[0]["refresh_token"] == "rt-2"
    entry = oa.cache_get("rt-2")
    assert entry["refresh_token"] == "rt-2-rotated"  # 轮转只改缓存，配置 API_KEY 不变
    assert entry["org_id"] == "org-2" and entry["allowed_models"] == ["glm-5.3-flash"]

    # 未临期命中缓存，不再刷新
    assert oa.get_auth_context({"API_KEY": "rt-2"})[0] == "fresh"
    assert len(calls) == 1


# ── 登录钩子 ──────────────────────────────────────────────────

def test_login_primes_cache_and_returns_refresh_token(plugin, monkeypatch):
    oa = plugin._load_oauth()
    monkeypatch.setattr(
        oa,
        "login",
        lambda cancel_event=None: {
            "access_token": "at-login",
            "refresh_token": "rt-login",
            "expires_in": 1800,
            "org_id": "org-a",
            "orgs": [{"id": "org-a", "name": "团队 A"}, {"id": "org-b", "name": "团队 B"}],
            "allowed_models": ["glm-5.3-flash"],
            "user_code": "ABCD-1234",
            "verification_uri": "https://opencode.ai/console/device",
        },
    )
    result = plugin._oauth_login(cancel_event=None)
    assert result["api_key"] == "rt-login"
    assert "已选第一个" in result["info"]
    assert oa.cache_get("rt-login")["access_token"] == "at-login"
    assert oa.cache_get("rt-login")["org_id"] == "org-a"


def test_login_falls_back_to_access_token_when_no_refresh(plugin, monkeypatch):
    oa = plugin._load_oauth()
    monkeypatch.setattr(
        oa,
        "login",
        lambda cancel_event=None: {
            "access_token": "at-only",
            "refresh_token": "",
            "expires_in": 1800,
            "org_id": "",
            "orgs": [],
            "allowed_models": [],
            "user_code": "",
            "verification_uri": "",
        },
    )
    result = plugin._oauth_login(cancel_event=None)
    assert result["api_key"] == "at-only"
    assert "未检测到 workspace" in result["info"]


# ── 可中断轮询 ────────────────────────────────────────────────

def test_login_is_cancellable(plugin, monkeypatch):
    oa = plugin._load_oauth()
    monkeypatch.setattr(
        oa,
        "start_device_flow",
        lambda timeout=None: {
            "device_code": "dc",
            "user_code": "CODE",
            "verification_uri_complete": "/console/device?user_code=CODE",
            "expires_in": 600,
            "interval": 0,
        },
    )
    monkeypatch.setattr(oa, "verification_url", lambda s: "https://opencode.ai/console/device")
    import webbrowser  # login() 内延迟 import，patch 标准库模块本身

    monkeypatch.setattr(webbrowser, "open", lambda u: None)
    monkeypatch.setattr(oa, "_copy_to_clipboard", lambda t: True)

    import threading

    stop = threading.Event()
    stop.set()
    with pytest.raises(oa.LoginCancelled):
        oa.login(cancel_event=stop)


# ── 模型池解析 ────────────────────────────────────────────────

def test_fetch_workspace_models_filters_unroutable_and_disabled(plugin, monkeypatch):
    oa = plugin._load_oauth()
    payload = {
        "config": {
            "provider": {
                "opencode": {
                    "models": {
                        "glm-5.3-flash": {"name": "GLM"},
                        "test-smoke": {"name": "internal"},
                        "deprecated-one": {"name": "old", "status": "deprecated"},
                        "disabled-one": {"name": "off", "disabled": True},
                        "gpt-6.1": {"provider": {"npm": "@ai-sdk/openai"}},
                        "claude-opus": {"provider": {"npm": "@ai-sdk/anthropic"}},
                    },
                    "whitelist": ["glm-5.3-flash", "deprecated-one", "gpt-6.1", "claude-opus"],
                },
                "byok": {
                    "options": {"baseURL": "https://external.example/v1", "apiKey": "x"},
                    "models": {"should-not-appear": {}},
                },
            }
        }
    }
    monkeypatch.setattr(oa, "_request", lambda url, **kw: payload)
    models = oa.fetch_workspace_models("at", "org")
    assert models == ["glm-5.3-flash"]


def test_fetch_models_hook_requires_login(plugin):
    with pytest.raises(RuntimeError, match="尚未登录"):
        plugin._oauth_fetch_models({"API_KEY": ""})


def test_fetch_models_hook_returns_workspace_models(plugin, monkeypatch):
    oa = plugin._load_oauth()
    oa.cache_put(
        "rt-3",
        access_token="at-3",
        refresh_token="rt-3",
        expires_at=time.time() + 3600,
        org_id="org-3",
    )
    monkeypatch.setattr(oa, "fetch_workspace_models", lambda access, org, timeout=None: ["glm-5.3-flash"])
    assert plugin._oauth_fetch_models({"API_KEY": "rt-3"}) == ["glm-5.3-flash"]


def test_go_models_hook_uses_public_catalog(plugin, monkeypatch):
    """Go (OAuth) 模型池走公开 lite 目录，不依赖 workspace /api/config。"""
    oa = plugin._load_oauth()
    seen = []
    monkeypatch.setattr(
        oa,
        "_request",
        lambda url, **kw: seen.append(url) or {
            "object": "list",
            "data": [
                {"id": "glm-5.3-flash", "api": {"chat-completions": {}}},
                {"id": "grok-4.7", "api": {"responses": {}}},
                {"id": "claude-haiku-5.5", "api": {"messages": {}}},
                {"id": "gemini-4-argon", "api": {"generate-content": {}}},
            ],
        },
    )
    assert plugin._go_oauth_fetch_models({}) == ["glm-5.3-flash"]
    assert seen[0] == "https://opencode.ai/inference/go/v1/models"


def test_start_device_flow_requests_org_scope(plugin, monkeypatch):
    """授权页绑定 workspace：token 交换回带的 org_id 优先于取第一个。"""
    oa = plugin._load_oauth()
    seen = []
    monkeypatch.setattr(
        oa,
        "_request",
        lambda url, **kw: seen.append(kw.get("body")) or {
            "device_code": "dc", "user_code": "CODE",
            "verification_uri_complete": "/console/device?user_code=CODE",
            "expires_in": 600, "interval": 0,
        },
    )
    oa.start_device_flow()
    assert seen[0]["supports_org_scope"] is True


# ── 宿主链路：resolve_extra_headers 端到端 ─────────────────────

def test_resolve_extra_headers_oauth_vs_api_key_isolation(plugin, monkeypatch, tmp_path):
    """真实走宿主 family_capabilities / resolve_extra_headers：

    OAuth 配置 → 动态 Authorization + org + session 头；
    API Key 版 Zen → 只有 session 头，绝不带动态 Authorization（独立 family 的意义）。
    """
    from app.core.modelmeta.provider_profile import resolve_extra_headers
    from app.plugins.registries.provider_registry import ProviderRegistry

    reg = ProviderRegistry()
    plugin.register(reg)
    monkeypatch.setattr(ProviderRegistry, "get_instance", staticmethod(lambda: reg))

    oa = plugin._load_oauth()
    oa._CACHE_DIR = tmp_path
    oa.cache_put(
        "rt-x",
        access_token="at-x",
        refresh_token="rt-x",
        expires_at=time.time() + 3600,
        org_id="org-x",
    )

    oauth_headers = resolve_extra_headers(
        {"provider_name": "OpenCode Go (OAuth)", "API_KEY": "rt-x", "模型名称": "glm-5.3-flash"},
        "sess-1",
    )
    assert oauth_headers["Authorization"] == "Bearer at-x"
    assert oauth_headers["x-opencode-org-id"] == "org-x"
    assert oauth_headers["x-opencode-session"] == "sess-1"
    assert oauth_headers["User-Agent"] == "opencode/cli"

    # API Key 版：family=opencode，不含 OAuth 插件的动态头
    zen_headers = resolve_extra_headers(
        {"provider_name": "OpenCode Zen", "API_KEY": "sk-zen", "模型名称": "glm-5"},
        "sess-2",
    )
    assert "Authorization" not in zen_headers
    assert "x-opencode-org-id" not in zen_headers
    assert zen_headers.get("x-opencode-session") == "sess-2"


# ── 端点探活口径（纯解析，不发请求）────────────────────────────

def test_device_flow_error_classification(plugin, monkeypatch):
    oa = plugin._load_oauth()
    # 绝对路径按 urljoin 语义归一到域名根，与参考实现 new URL(..., CONSOLE+"/") 一致
    assert oa.verification_url({"verification_uri_complete": "/console/device?user_code=X"}) == (
        "https://opencode.ai/console/device?user_code=X"
    )
    assert oa.verification_url({"verification_uri": "https://opencode.ai/console/device"}) == (
        "https://opencode.ai/console/device"
    )
