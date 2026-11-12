# -*- coding: utf-8 -*-
"""model.complete：会话头注入 / 模型解析 / 失败降级。

背景：插件自建 client 拿不到 worker 侧的请求头组装，OpenCode 网关缺
x-opencode-session 直接 400 MissingSessionID（system-question 代答实测）。
能力面统一收口后，请求头必须由 resolve_extra_headers 注入。
"""
import pytest

from app.plugins.capabilities.providers import ui_core


class _Session:
    session_id = "sess-42"


class _Backend:
    def get_current_session(self):
        return _Session()


class _Host:
    def __init__(self, cfg=None, resolved=None):
        self.cfg = cfg or {}
        self.resolved = resolved
        self.backend = _Backend()

    def _get_current_model_config(self):
        return dict(self.cfg)

    def _resolve_model_config_by_query(self, query, show_error=True, capability_type="chat"):
        return dict(self.resolved or {})


@pytest.fixture()
def calls(monkeypatch):
    """替换 http 层：记录 client 参数与最终 messages。"""
    recorded = {}

    def fake_build(api_key, base_url, timeout=None, extra_headers=None):
        recorded["client"] = {
            "api_key": api_key,
            "base_url": base_url,
            "timeout": timeout,
            "extra_headers": extra_headers,
        }
        return "client"

    def fake_text(client, *, model, messages, temperature=None, max_tokens=None, top_p=None):
        recorded["text"] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        return '{"answers": []}'

    monkeypatch.setattr("app.utils.http_client.build_openai_client", fake_build)
    monkeypatch.setattr("app.utils.http_client.chat_completion_text", fake_text)
    return recorded


def test_complete_follows_current_model_and_injects_session_header(calls, monkeypatch):
    seen = {}

    def fake_headers(cfg, session_id=""):
        seen["cfg"] = dict(cfg)
        seen["session_id"] = session_id
        return {"x-opencode-session": session_id or "none"}

    monkeypatch.setattr("app.core.modelmeta.provider_profile.resolve_extra_headers", fake_headers)
    host = _Host(cfg={"模型名称": "deepseek-v4.1-flash", "API_URL": "https://opencode.ai/x", "API_KEY": "k"})

    out = ui_core._model_complete(host)("hi", system="SYS")

    assert out == '{"answers": []}'
    assert seen["session_id"] == "sess-42"
    assert calls["client"]["extra_headers"] == {"x-opencode-session": "sess-42"}
    assert calls["client"]["base_url"] == "https://opencode.ai/x"
    assert calls["text"]["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "hi"},
    ]


def test_complete_with_model_query_uses_resolver(calls, monkeypatch):
    monkeypatch.setattr("app.core.modelmeta.provider_profile.resolve_extra_headers", lambda *a, **k: {})
    host = _Host(
        cfg={"模型名称": "cur-model", "API_URL": "https://a"},
        resolved={"模型名称": "glm-5", "API_URL": "https://b"},
    )

    assert ui_core._model_complete(host)("q", model_query="glm-5") == '{"answers": []}'
    assert calls["text"]["model"] == "glm-5"
    assert calls["client"]["base_url"] == "https://b"


def test_complete_returns_empty_without_complete_config(calls):
    host = _Host(cfg={"模型名称": "m"})  # 缺 API_URL

    assert ui_core._model_complete(host)("q") == ""
    assert "client" not in calls


def test_complete_returns_empty_without_host(calls, monkeypatch):
    monkeypatch.setattr("app.plugins.capabilities.resolve.active_main_widget", lambda: None)
    assert ui_core._model_complete(None)("q") == ""
    assert "client" not in calls


def test_complete_swallows_call_error(calls, monkeypatch):
    monkeypatch.setattr("app.core.modelmeta.provider_profile.resolve_extra_headers", lambda *a, **k: {})

    def text_boom(*args, **kwargs):
        raise RuntimeError("400 MissingSessionID")

    monkeypatch.setattr("app.utils.http_client.chat_completion_text", text_boom)
    host = _Host(cfg={"模型名称": "m", "API_URL": "https://x"})

    assert ui_core._model_complete(host)("q") == ""
