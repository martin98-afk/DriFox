# -*- coding: utf-8 -*-
"""CodeBuddy providers 插件：声明、family 探测与 extra_headers 注入链路。

覆盖三段：
1. 插件声明侧 —— ProviderDef 注册、伪装身份头四件套（capabilities["extra_headers"]）
2. family 探测 —— copilot.tencent.com 必须判为 codebuddy（其模型池含
   glm-*/deepseek-* 系，按模型名前缀会误判成 zhipu/deepseek 族，回归点）
3. worker 注入侧 —— chat_worker._provider_extra_headers 端到端注入静态头
"""

import importlib.util
from pathlib import Path

import pytest

from app.core.modelmeta.provider_profile import detect_provider_family, get_provider_profile
from app.plugins.registries.provider_registry import ProviderRegistry

_PROVIDER_FILE = Path("plugins/system-providers/providers/codebuddy.py")

pytestmark = pytest.mark.skipif(
    not _PROVIDER_FILE.exists(), reason="codebuddy-provider 插件未安装"
)


def _load_codebuddy_plugin():
    spec = importlib.util.spec_from_file_location("codebuddy_provider_plugin", _PROVIDER_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def fresh_provider_registry(monkeypatch):
    """每用例独立 registry（绕过单例状态污染）"""
    reg = ProviderRegistry()
    monkeypatch.setattr(ProviderRegistry, "get_instance", staticmethod(lambda: reg))
    return reg


# ── 插件声明侧 ──────────────────────────────────────────


def test_declares_codebuddy_provider(fresh_provider_registry):
    """插件按 ProviderDef 注册 CodeBuddy 服务商"""
    _load_codebuddy_plugin().register(fresh_provider_registry)
    provider = fresh_provider_registry.get("CodeBuddy")
    assert provider is not None
    assert provider.family == "codebuddy"
    assert provider.api_url == "https://copilot.tencent.com/v2"
    assert provider.auth_type == "bearer"
    assert "glm-5.3-flash" in provider.models


def test_capabilities_carry_disguise_headers(fresh_provider_registry):
    """capabilities 声明伪装身份头四件套"""
    _load_codebuddy_plugin().register(fresh_provider_registry)
    caps = fresh_provider_registry.family_capabilities("codebuddy")
    extra = caps.get("extra_headers")
    assert isinstance(extra, dict)
    assert extra["User-Agent"] == "CodeBuddyIDE/1.106.1"
    assert extra["X-Product"] == "SaaS"
    assert extra["X-Product-Code"] == "codebuddy"
    assert extra["X-Domain"] == "copilot.tencent.com"


def test_declares_login_hook(fresh_provider_registry):
    """capabilities["login_hook"] / ["models_hook"] 可调用"""
    _load_codebuddy_plugin().register(fresh_provider_registry)
    caps = fresh_provider_registry.family_capabilities("codebuddy")
    assert callable(caps.get("login_hook"))
    assert callable(caps.get("models_hook"))


# ── family 探测 ─────────────────────────────────────────


def test_detect_family_by_url_beats_model_prefix():
    """URL 命中 codebuddy 时优先于 glm/deepseek 模型名前缀（误判回归点）"""
    cfg = {"API_URL": "https://copilot.tencent.com/v2", "模型名称": "glm-5.3-flash"}
    assert detect_provider_family(cfg) == "codebuddy"
    cfg = {"API_URL": "https://copilot.tencent.com/v2", "模型名称": "deepseek-v4-pro"}
    assert detect_provider_family(cfg) == "codebuddy"


def test_detect_family_other_providers_unaffected():
    """既有服务商探测不受新分支影响"""
    assert detect_provider_family({"API_URL": "https://api.deepseek.com/v1"}) == "deepseek"
    assert detect_provider_family({"API_URL": "https://open.bigmodel.cn/api"}) == "zhipu"
    assert detect_provider_family({"API_URL": "https://api.example.com/v1"}) == "custom"


def test_profile_aggregates_extra_headers(fresh_provider_registry):
    """get_provider_profile 端到端：glm 模型 + codebuddy URL → 聚合出伪装头"""
    _load_codebuddy_plugin().register(fresh_provider_registry)
    profile = get_provider_profile(
        {"API_URL": "https://copilot.tencent.com/v2", "模型名称": "glm-5.3-flash"}
    )
    assert profile["family"] == "codebuddy"
    assert profile["extra_headers"]["User-Agent"] == "CodeBuddyIDE/1.106.1"


# ── worker 注入侧 ───────────────────────────────────────

from app.core.workers.chat_worker import OpenAIChatWorker as ChatWorker  # noqa: E402


class _StubWorker:
    """只挂被测方法的轻量 stub（绕开 QThread 实例化）"""

    _provider_extra_headers = ChatWorker._provider_extra_headers

    def __init__(self, llm_config, session_id):
        self.llm_config = llm_config
        self.session_id = session_id


@pytest.fixture()
def patched_profile(monkeypatch):
    """替换 chat_worker 模块内的 get_provider_profile 引用，隔离全局 registry"""

    def _install(profile):
        monkeypatch.setattr(
            "app.core.workers.chat_worker.get_provider_profile", lambda cfg: profile
        )
        return _StubWorker

    return _install


def test_worker_injects_disguise_headers(patched_profile):
    """声明 extra_headers 的 provider → 静态头注入，不依赖会话 ID"""
    stub = patched_profile(
        {
            "extra_headers": {
                "User-Agent": "CodeBuddyIDE/1.106.1",
                "X-Product-Code": "codebuddy",
            }
        }
    )({"API_URL": "https://copilot.tencent.com/v2"}, "")
    assert stub._provider_extra_headers() == {
        "User-Agent": "CodeBuddyIDE/1.106.1",
        "X-Product-Code": "codebuddy",
    }


def test_worker_merges_disguise_and_session_headers(patched_profile):
    """伪装头与会话头并存 → 合并注入"""
    stub = patched_profile(
        {
            "extra_headers": {"User-Agent": "CodeBuddyIDE/1.106.1"},
            "session_header": "x-gateway-session",
        }
    )({"API_URL": "https://copilot.tencent.com/v2"}, "sess-1")
    assert stub._provider_extra_headers() == {
        "User-Agent": "CodeBuddyIDE/1.106.1",
        "x-gateway-session": "sess-1",
    }
