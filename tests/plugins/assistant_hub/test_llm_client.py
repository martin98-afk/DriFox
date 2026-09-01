# -*- coding: utf-8 -*-
"""test_llm_client.py — assistant_hub core/llm_client 单元测试。"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "core" / "llm_client.py"


def _load():
    spec = importlib.util.spec_from_file_location("test_llm_client_mod", str(_MODULE))
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("test_llm_client_mod", mod)
    spec.loader.exec_module(mod)
    return mod


m = _load()


class _FakeResp:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self._raw


def test_chat_once_builds_request_and_parses(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=60):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        captured["headers"] = dict(req.header_items())
        return _FakeResp({"choices": [{"message": {"content": "  你好  "}}]})

    monkeypatch.setattr(m, "resolve_model_config", lambda config_id="": {
        "base_url": "https://api.test.com/v1", "api_key": "sk-x",
        "model": "m1", "provider_name": "Test",
    })
    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)

    out = m.chat_once([{"role": "user", "content": "hi"}])
    assert out == "你好"
    assert captured["url"] == "https://api.test.com/v1/chat/completions"
    assert captured["body"]["model"] == "m1"
    assert captured["body"]["stream"] is False
    # urllib 会规范化 Authorization 头键名
    assert any(k.lower() == "authorization" for k in captured["headers"])
    assert any(v == "Bearer sk-x" for v in captured["headers"].values())


def test_chat_once_empty_key_no_auth_header(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=60):
        seen["headers"] = {k.lower() for k, _v in req.header_items()}
        return _FakeResp({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(m, "resolve_model_config", lambda config_id="": {
        "base_url": "https://api.test.com/v1", "api_key": "",
        "model": "m1", "provider_name": "Test",
    })
    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    assert m.chat_once([{"role": "user", "content": "hi"}]) == "ok"
    assert "authorization" not in seen["headers"]


def test_chat_once_overrides(monkeypatch):
    def fake_urlopen(req, timeout=60):
        body = json.loads(req.data.decode())
        assert body["model"] == "override-m"
        assert body["temperature"] == 0.7
        assert req.full_url == "https://other.test/v1/chat/completions"
        return _FakeResp({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(m, "resolve_model_config", lambda config_id="": {
        "base_url": "https://api.test.com/v1", "api_key": "k",
        "model": "m1", "provider_name": "T",
    })
    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    m.chat_once([{"role": "user", "content": "x"}], model="override-m",
                temperature=0.7, base_url="https://other.test/v1")


def test_resolve_model_config_missing(monkeypatch):
    class _Cfg:
        class llm_selected_model:
            value = ""

        class llm_saved_providers:
            value = {}

    monkeypatch.setattr(m, "_settings", lambda: _Cfg())
    with pytest.raises(m.LLMUnavailableError):
        m.resolve_model_config()
