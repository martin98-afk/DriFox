# -*- coding: utf-8 -*-
"""三家 OAuth/凭据型插件 health_probe 钩子回归测试。

覆盖（每家同构四态）：
1. ProviderDef 声明 capabilities["health_probe"] 可调用
2. 未登录（API_KEY 空）→ auth_failed
3. 凭据换取失败（refresh 链废 / gho 失效）→ auth_failed
4. 网关 401 → auth_failed（凭据被吊销是列表探测永远看不到的失效）
5. HTTP 200 → ok；网络异常 → unreachable

不调 register（CodeBuddy register 会起守护刷新线程）；直接调模块级钩子。
urlopen 全部 mock，无网络。
"""
import importlib.util
import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_PROVIDERS = Path("plugins/system-providers/providers")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b"{}"


def _urlopen_ok(req, **kwargs):
    return _FakeResp()


def _urlopen_http_error(code: int):
    def fake(req, **kwargs):
        raise urllib.error.HTTPError(req.full_url, code, "err", hdrs=None, fp=None)

    return fake


def _urlopen_os_error(req, **kwargs):
    raise OSError("connection refused")


# ── OpenCode（Zen/Go OAuth）─────────────────────────────

def _stub_opencode_oauth(monkeypatch, mod, access="acc", org="org1"):
    oa = SimpleNamespace(
        get_auth_context=lambda cfg: (access, org),
        _ssl_context=lambda: None,
    )
    monkeypatch.setattr(mod, "_load_oauth", lambda: oa)


def test_opencode_declares_health_probe(monkeypatch):
    mod = _load("opencode_probe_t", _PROVIDERS / "opencode.py")
    caps = mod._oauth_capabilities(lambda cfg: ["m1"])
    assert callable(caps.get("health_probe"))


def test_opencode_probe_ok(monkeypatch):
    import urllib.request as _ur

    mod = _load("opencode_probe_ok", _PROVIDERS / "opencode.py")
    _stub_opencode_oauth(monkeypatch, mod)
    monkeypatch.setattr(_ur, "urlopen", _urlopen_ok)
    status = mod._oauth_health_probe(
        {"API_KEY": "rt", "API_URL": "https://opencode.ai/inference/openai/v1", "模型名称": "m1"}
    )
    assert status == "ok"


def test_opencode_probe_401_auth_failed(monkeypatch):
    import urllib.request as _ur

    mod = _load("opencode_probe_401", _PROVIDERS / "opencode.py")
    _stub_opencode_oauth(monkeypatch, mod)
    monkeypatch.setattr(_ur, "urlopen", _urlopen_http_error(401))
    status = mod._oauth_health_probe(
        {"API_KEY": "rt", "API_URL": "https://opencode.ai/inference/openai/v1", "模型名称": "m1"}
    )
    assert status == "auth_failed"


def test_opencode_probe_not_logged_in(monkeypatch):
    mod = _load("opencode_probe_nokey", _PROVIDERS / "opencode.py")
    assert mod._oauth_health_probe({"API_KEY": "", "模型名称": "m1"}) == "auth_failed"


def test_opencode_probe_dead_refresh_chain(monkeypatch):
    mod = _load("opencode_probe_dead", _PROVIDERS / "opencode.py")
    oa = SimpleNamespace(
        get_auth_context=lambda cfg: (_ for _ in ()).throw(RuntimeError("invalid_grant")),
        _ssl_context=lambda: None,
    )
    monkeypatch.setattr(mod, "_load_oauth", lambda: oa)
    assert mod._oauth_health_probe({"API_KEY": "rt", "模型名称": "m1"}) == "auth_failed"


def test_opencode_probe_network_error(monkeypatch):
    import urllib.request as _ur

    mod = _load("opencode_probe_net", _PROVIDERS / "opencode.py")
    _stub_opencode_oauth(monkeypatch, mod)
    monkeypatch.setattr(_ur, "urlopen", _urlopen_os_error)
    status = mod._oauth_health_probe(
        {"API_KEY": "rt", "API_URL": "https://opencode.ai/inference/openai/v1", "模型名称": "m1"}
    )
    assert status == "unreachable"


# ── GitHub Copilot ──────────────────────────────────────

def _stub_copilot_auth(monkeypatch, mod, exchange=None):
    monkeypatch.setattr(
        mod, "_exchange_copilot_token", exchange or (lambda gho: "cpt-token")
    )
    ca = SimpleNamespace(build_chat_headers=lambda t: {"Authorization": f"Bearer {t}"})
    monkeypatch.setattr(mod, "_load_auth_module", lambda: ca)
    monkeypatch.setattr(mod, "_system_ssl_context", lambda: None)


def test_copilot_declares_health_probe():
    mod = _load("copilot_probe_decl", _PROVIDERS / "github_copilot.py")
    # register 不调（无 registry 依赖风险），直接看 capabilities 组装所在模块函数
    # Copilot 的 capabilities 在 register 内联，改为跑一次轻量断言：
    assert hasattr(mod, "_health_probe") and callable(mod._health_probe)


def test_copilot_probe_ok(monkeypatch):
    import urllib.request as _ur

    mod = _load("copilot_probe_ok", _PROVIDERS / "github_copilot.py")
    _stub_copilot_auth(monkeypatch, mod)
    monkeypatch.setattr(_ur, "urlopen", _urlopen_ok)
    assert mod._health_probe({"API_KEY": "gho", "模型名称": "gpt-4o"}) == "ok"


def test_copilot_probe_403_auth_failed(monkeypatch):
    import urllib.request as _ur

    mod = _load("copilot_probe_403", _PROVIDERS / "github_copilot.py")
    _stub_copilot_auth(monkeypatch, mod)
    monkeypatch.setattr(_ur, "urlopen", _urlopen_http_error(403))
    assert mod._health_probe({"API_KEY": "gho", "模型名称": "gpt-4o"}) == "auth_failed"


def test_copilot_probe_gho_dead(monkeypatch):
    mod = _load("copilot_probe_dead", _PROVIDERS / "github_copilot.py")
    _stub_copilot_auth(monkeypatch, mod, exchange=lambda gho: (_ for _ in ()).throw(RuntimeError("bad gho")))
    assert mod._health_probe({"API_KEY": "gho", "模型名称": "gpt-4o"}) == "auth_failed"


# ── CodeBuddy ───────────────────────────────────────────

def _stub_codebuddy_auth(monkeypatch, mod, exchange=None):
    monkeypatch.setattr(mod, "_mark_active", lambda rt: None)
    monkeypatch.setattr(mod, "_exchange_access_token", exchange or (lambda rt: "acc-token"))
    ba = SimpleNamespace(
        build_headers=lambda t: {"Authorization": f"Bearer {t}"},
        CHAT_COMPLETIONS_PATH="/v2/chat/completions",
    )
    monkeypatch.setattr(mod, "_load_auth_module", lambda: ba)
    monkeypatch.setattr(mod, "_system_ssl_context", lambda: None)


def test_codebuddy_probe_ok(monkeypatch):
    import urllib.request as _ur

    mod = _load("codebuddy_probe_ok", _PROVIDERS / "codebuddy.py")
    _stub_codebuddy_auth(monkeypatch, mod)
    seen = {}

    def fake_urlopen(req, **kwargs):
        seen["url"] = req.full_url
        return _FakeResp()

    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)
    status = mod._health_probe(
        {"API_KEY": "rt", "API_URL": "https://copilot.tencent.com/v2", "模型名称": "glm-5.3-flash"}
    )
    assert status == "ok"
    assert seen["url"] == "https://copilot.tencent.com/v2/chat/completions"


def test_codebuddy_probe_401_auth_failed(monkeypatch):
    import urllib.request as _ur

    mod = _load("codebuddy_probe_401", _PROVIDERS / "codebuddy.py")
    _stub_codebuddy_auth(monkeypatch, mod)
    monkeypatch.setattr(_ur, "urlopen", _urlopen_http_error(401))
    assert mod._health_probe({"API_KEY": "rt", "模型名称": "m1"}) == "auth_failed"


def test_codebuddy_probe_not_logged_in():
    mod = _load("codebuddy_probe_nokey", _PROVIDERS / "codebuddy.py")
    assert mod._health_probe({"API_KEY": "", "模型名称": "m1"}) == "auth_failed"


def test_codebuddy_probe_dead_refresh(monkeypatch):
    mod = _load("codebuddy_probe_dead", _PROVIDERS / "codebuddy.py")
    _stub_codebuddy_auth(monkeypatch, mod, exchange=lambda rt: (_ for _ in ()).throw(RuntimeError("refresh failed")))
    assert mod._health_probe({"API_KEY": "rt", "模型名称": "m1"}) == "auth_failed"


def test_codebuddy_declares_health_probe_after_register(monkeypatch):
    """register 后 ProviderDef.capabilities 携带 health_probe（守护线程以环境变量关闭）"""
    from app.plugins.registries.provider_registry import ProviderRegistry

    monkeypatch.setenv("DRIFOX_NO_CODEBUDDY_REFRESH", "1")
    mod = _load("codebuddy_probe_decl", _PROVIDERS / "codebuddy.py")
    reg = ProviderRegistry()
    mod.register(reg)
    caps = reg.get("CodeBuddy").capabilities
    assert callable(caps.get("health_probe"))


# ── 探活模型透传（服务端挑最便宜模型，经「探活模型」键传入钩子） ──

def test_opencode_probe_uses_probe_model(monkeypatch):
    """config 带「探活模型」→ 请求体 model 用它（不用配置的贵模型）"""
    import urllib.request as _ur

    mod = _load("opencode_probe_pm", _PROVIDERS / "opencode.py")
    _stub_opencode_oauth(monkeypatch, mod)
    seen = {}

    def fake_urlopen(req, **kwargs):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)
    mod._oauth_health_probe(
        {
            "API_KEY": "rt",
            "API_URL": "https://opencode.ai/inference/openai/v1",
            "模型名称": "claude-opus-4.1",
            "探活模型": "mimo-v2.5-free",
        }
    )
    assert seen["body"]["model"] == "mimo-v2.5-free"


def test_copilot_probe_uses_probe_model(monkeypatch):
    import urllib.request as _ur

    mod = _load("copilot_probe_pm", _PROVIDERS / "github_copilot.py")
    _stub_copilot_auth(monkeypatch, mod)
    seen = {}

    def fake_urlopen(req, **kwargs):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)
    mod._health_probe({"API_KEY": "gho", "模型名称": "claude-opus-4.1", "探活模型": "gpt-4o-mini"})
    assert seen["body"]["model"] == "gpt-4o-mini"


def test_codebuddy_probe_uses_probe_model(monkeypatch):
    import urllib.request as _ur

    mod = _load("codebuddy_probe_pm", _PROVIDERS / "codebuddy.py")
    _stub_codebuddy_auth(monkeypatch, mod)
    seen = {}

    def fake_urlopen(req, **kwargs):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)
    mod._health_probe({"API_KEY": "rt", "模型名称": "glm-5.3", "探活模型": "glm-5.3-flash"})
    assert seen["body"]["model"] == "glm-5.3-flash"


def test_plugins_fallback_to_current_model_without_probe_key(monkeypatch):
    """无「探活模型」键（旧调用方）→ 回退「模型名称」，行为不变"""
    import urllib.request as _ur

    mod = _load("codebuddy_probe_fb", _PROVIDERS / "codebuddy.py")
    _stub_codebuddy_auth(monkeypatch, mod)
    seen = {}

    def fake_urlopen(req, **kwargs):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    monkeypatch.setattr(_ur, "urlopen", fake_urlopen)
    mod._health_probe({"API_KEY": "rt", "模型名称": "glm-5.3"})
    assert seen["body"]["model"] == "glm-5.3"
