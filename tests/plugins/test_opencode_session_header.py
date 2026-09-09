# -*- coding: utf-8 -*-
"""OpenCode 网关会话头（x-opencode-session）声明与注入链路。

背景：OpenCode Zen/Go 网关自 2026-09-06 起要求每个 LLM 请求携带
x-opencode-session（稳定会话 ID），缺失报 400 MissingSessionID。
头名由 providers 插件 opencode.py 按 family 声明（capabilities["session_header"]），
chat_worker._gateway_session_headers 通用注入，值=当前会话 ID。
"""

import pytest

from app.core.provider_profile import get_provider_profile
from app.plugins.registries.provider_registry import ProviderRegistry


# ── 插件声明侧 ──────────────────────────────────────────


@pytest.fixture()
def fresh_provider_registry(monkeypatch):
    """每用例独立 registry（绕过单例状态污染）"""
    reg = ProviderRegistry()
    monkeypatch.setattr(ProviderRegistry, "get_instance", staticmethod(lambda: reg))
    return reg


def _load_opencode_plugin(registry):
    from importlib import import_module
    opencode_plugin = import_module("plugins.system-providers.providers.opencode")

    opencode_plugin.register(registry)


def test_opencode_plugin_declares_session_header(fresh_provider_registry):
    """opencode 插件按 family 声明 session_header 能力"""
    _load_opencode_plugin(fresh_provider_registry)
    caps = fresh_provider_registry.family_capabilities("opencode")
    assert caps.get("session_header") == "x-opencode-session"


def test_provider_profile_exposes_session_header(fresh_provider_registry):
    """get_provider_profile 经 family 聚合透出 session_header（端到端）"""
    _load_opencode_plugin(fresh_provider_registry)
    profile = get_provider_profile({"API_URL": "https://opencode.ai/zen/go/v1"})
    assert profile["family"] == "opencode"
    assert profile["session_header"] == "x-opencode-session"


# ── worker 注入侧 ───────────────────────────────────────

from app.core.workers.chat_worker import OpenAIChatWorker as ChatWorker  # noqa: E402


class _StubWorker:
    """只挂被测方法的轻量 stub（绕开 QThread 实例化）"""

    _gateway_session_headers = ChatWorker._gateway_session_headers

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


def test_injects_session_header_for_declared_provider(patched_profile):
    """声明了 session_header 的 provider → 注入 {header: session_id}"""
    stub = patched_profile({"session_header": "x-opencode-session"})(
        {"API_URL": "https://opencode.ai/zen/go/v1"}, "sess-abc"
    )
    assert stub._gateway_session_headers() == {"x-opencode-session": "sess-abc"}


def test_no_session_id_no_header(patched_profile):
    """无会话 ID（理论上不出现）→ 不注入，不发明假 ID"""
    stub = patched_profile({"session_header": "x-opencode-session"})(
        {"API_URL": "https://opencode.ai/zen/go/v1"}, ""
    )
    assert stub._gateway_session_headers() is None


def test_undeclared_provider_no_header(patched_profile):
    """未声明 session_header 的 provider → 不注入"""
    stub = patched_profile({})({"API_URL": "https://api.deepseek.com/v1"}, "sess-abc")
    assert stub._gateway_session_headers() is None


def test_profile_exception_safe(monkeypatch):
    """profile 解析拖异常 → 返回 None 不影响请求构建"""

    def _boom(cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.core.workers.chat_worker.get_provider_profile", _boom)
    stub = _StubWorker({"API_URL": "https://opencode.ai/zen/go/v1"}, "sess-abc")
    assert stub._gateway_session_headers() is None
