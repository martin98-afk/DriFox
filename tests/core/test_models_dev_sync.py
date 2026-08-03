# -*- coding: utf-8 -*-
"""models.dev 同步模块的单元测试。"""

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from app.core import models_dev_sync as sync


# ============================================================
# _transform_model
# ============================================================
def test_transform_model_basic():
    info = {
        "description": "Test model",
        "release_date": "2026-01-01",
        "modalities": {"input": ["text"], "output": ["text"]},
        "limit": {"context": 128000, "output": 4096},
        "reasoning": True,
        "reasoning_options": [{"type": "effort"}],
    }
    result = sync._transform_model("openai", "gpt-test", info)
    assert result is not None
    assert result["context_limit"] == 128000
    assert result["max_output_tokens"] == 4096
    assert result["supports_vision"] is False
    assert result["supports_thinking"] is True
    assert result["thinking_param"] == "reasoning_effort"
    assert result["source"] == "models.dev"


def test_transform_model_toggle():
    info = {
        "modalities": {"input": ["text", "image"], "output": ["text"]},
        "limit": {"context": 64000},
        "reasoning": True,
        "reasoning_options": [{"type": "toggle"}],
    }
    result = sync._transform_model("zhipuai", "glm-test", info)
    assert result["supports_vision"] is True
    assert result["thinking_param"] == "thinking"
    assert "max_output_tokens" not in result


def test_transform_model_no_reasoning():
    info = {
        "modalities": {"input": ["text"], "output": ["text"]},
        "limit": {"context": 8192, "output": 4096},
        "reasoning": False,
    }
    result = sync._transform_model("openai", "gpt-test", info)
    assert result["supports_thinking"] is False
    assert "thinking_param" not in result


def test_transform_model_reasoning_without_options_no_controls():
    """reasoning=True 但 reasoning_options=[] → 模型不可控，应返回 supports_thinking=False"""
    info = {
        "modalities": {"input": ["text"], "output": ["text"]},
        "limit": {"context": 200000},
        "reasoning": True,
        "reasoning_options": [],
    }
    result = sync._transform_model("opencode", "some-reasoning-model", info)
    assert result["supports_thinking"] is False
    assert "thinking_param" not in result


def test_transform_model_missing_context():
    info = {
        "modalities": {"input": ["text"], "output": ["text"]},
        "limit": {"output": 4096},
        "reasoning": False,
    }
    assert sync._transform_model("openai", "gpt-test", info) is None


# ============================================================
# _parse_models_dev_data
# ============================================================
def test_parse_models_dev_data_filters_whitelist():
    data = {
        "openai": {
            "models": {
                "gpt-test": {
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 128000},
                    "reasoning": True,
                    "reasoning_options": [{"type": "effort"}],
                }
            }
        },
        "opencode": {
            "models": {
                "opencode-test": {
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 200000},
                    "reasoning": True,
                    "reasoning_options": [],
                }
            }
        },
        "unknown-provider": {
            "models": {
                "some-model": {
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 128000},
                    "reasoning": False,
                }
            }
        },
    }
    provider_models, caps = sync._parse_models_dev_data(data)
    assert "OpenAI" in provider_models
    assert "OpenCode Zen" in provider_models
    assert "gpt-test" in provider_models["OpenAI"]
    assert "opencode-test" in provider_models["OpenCode Zen"]
    assert "unknown-provider" not in provider_models
    assert caps["gpt-test"]["thinking_param"] == "reasoning_effort"
    # opencode-test: reasoning=True 但 reasoning_options=[] → 不可控，无 thinking_param
    assert "thinking_param" not in caps["opencode-test"]
    assert caps["opencode-test"]["supports_thinking"] is False


# ============================================================
# cache helpers
# ============================================================
def test_is_cache_valid(tmp_path: Path):
    valid = {"_cached_at": time.time()}
    invalid = {"_cached_at": time.time() - sync.CACHE_TTL_SECONDS - 1}
    missing = {}
    assert sync._is_cache_valid(valid) is True
    assert sync._is_cache_valid(invalid) is False
    assert sync._is_cache_valid(missing) is False
    assert sync._is_cache_valid(None) is False


def test_save_and_load_cache(tmp_path: Path):
    path = tmp_path / "cache.json"
    data = {"_cached_at": time.time(), "provider_models": {}, "model_capabilities": {}}
    sync._save_cache(data, path)
    loaded = sync._load_cache(path)
    assert loaded is not None
    assert loaded["_cached_at"] == data["_cached_at"]


# ============================================================
# load_dynamic_models
# ============================================================
def test_load_dynamic_models_uses_cache_when_valid(monkeypatch, tmp_path: Path):
    cache = {
        "_cached_at": time.time(),
        "provider_models": {"OpenAI": ["gpt-cached"]},
        "model_capabilities": {"gpt-cached": {"context_limit": 123}},
    }
    sync._save_cache(cache, tmp_path / "cache.json")

    fetch_called = False

    def _fake_fetch():
        nonlocal fetch_called
        fetch_called = True
        return None

    monkeypatch.setattr(sync, "_fetch_remote", _fake_fetch)
    result = sync.load_dynamic_models(cache_path=tmp_path / "cache.json")
    assert fetch_called is False
    assert result.provider_models["OpenAI"] == ["gpt-cached"]


def test_load_dynamic_models_fetches_when_expired(monkeypatch, tmp_path: Path):
    expired = {
        "_cached_at": time.time() - sync.CACHE_TTL_SECONDS - 1,
        "provider_models": {"OpenAI": ["gpt-old"]},
        "model_capabilities": {},
    }
    sync._save_cache(expired, tmp_path / "cache.json")

    remote_data = {
        "openai": {
            "models": {
                "gpt-new": {
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 128000},
                    "reasoning": False,
                }
            }
        }
    }
    monkeypatch.setattr(sync, "_fetch_remote", lambda: remote_data)
    result = sync.load_dynamic_models(cache_path=tmp_path / "cache.json")
    assert "gpt-new" in result.provider_models["OpenAI"]


def test_load_dynamic_models_fallback_to_stale_cache(monkeypatch, tmp_path: Path):
    stale = {
        "_cached_at": time.time() - sync.CACHE_TTL_SECONDS - 1,
        "provider_models": {"OpenAI": ["gpt-stale"]},
        "model_capabilities": {},
    }
    sync._save_cache(stale, tmp_path / "cache.json")
    monkeypatch.setattr(sync, "_fetch_remote", lambda: None)
    result = sync.load_dynamic_models(cache_path=tmp_path / "cache.json")
    assert result.provider_models["OpenAI"] == ["gpt-stale"]
    assert result.from_cache is True


def test_load_dynamic_models_empty_when_no_cache_and_fetch_fails(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(sync, "_fetch_remote", lambda: None)
    result = sync.load_dynamic_models(cache_path=tmp_path / "cache.json")
    assert result.provider_models == {}
    assert result.model_capabilities == {}


# ============================================================
# get_merged_provider_models
# ============================================================
def test_get_merged_provider_models_deduplicates_and_keeps_static_first(monkeypatch):
    dynamic = sync.DynamicModelsResult(
        provider_models={
            "OpenAI": ["gpt-4o", "gpt-new"],
        },
        model_capabilities={},
        from_cache=False,
        fetched_at=None,
    )
    monkeypatch.setattr(sync, "get_dynamic_models", lambda: dynamic)
    from app.constants import get_merged_provider_models

    merged = get_merged_provider_models()
    openai_models = merged["OpenAI"]
    # 静态模型在前
    assert openai_models[0] == "gpt-4o"
    # 去重：gpt-4o 只出现一次
    assert openai_models.count("gpt-4o") == 1
    assert "gpt-new" in openai_models
    # 静态模型顺序不变
    assert openai_models[:5] == ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo"]


def test_get_merged_provider_models_fallback_on_sync_exception(monkeypatch):
    def _raise():
        raise RuntimeError("sync broken")

    monkeypatch.setattr(sync, "get_dynamic_models", _raise)
    from app.constants import get_merged_provider_models, PROVIDER_MODELS

    merged = get_merged_provider_models()
    assert merged == PROVIDER_MODELS


# ============================================================
# fetch_opencode_free_models_for_providers（缓存 + in-flight 去重）
# ============================================================
@pytest.fixture(autouse=True)
def _clean_opencode_free_cache():
    """清理模块级缓存，避免测试间互相污染。"""
    with sync._OPENCODE_FREE_CACHE_LOCK:
        sync._OPENCODE_FREE_CACHE.clear()
        sync._OPENCODE_FREE_INFLIGHT.clear()
    yield
    with sync._OPENCODE_FREE_CACHE_LOCK:
        sync._OPENCODE_FREE_CACHE.clear()
        sync._OPENCODE_FREE_INFLIGHT.clear()


class _FakeResponse:
    """模拟 httpx 响应对象。"""

    def __init__(self, data, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data


class _FakeClient:
    """模拟 httpx.Client：记录每次 GET 请求，返回预设响应。"""

    def __init__(self, responses):
        self._responses = responses
        self.get_calls: list[tuple[str, Dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, headers=None):
        self.get_calls.append((url, headers or {}))
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def _fake_httpx_client_factory(monkeypatch, responses):
    """用 _FakeClient 替换 httpx.Client，返回 fake 客户端实例供断言。"""
    fake = _FakeClient(responses)

    class _FakeModule:
        Client = lambda *a, **kw: fake
        Timeout = lambda *a, **kw: kw.get("timeout", a[0] if a else None)

    monkeypatch.setitem(sys.modules, "httpx", _FakeModule())
    return fake


def test_fetch_opencode_free_models_cache_hit(monkeypatch):
    """同一实例在 TTL 窗口内第二次调用不再发网络请求。"""
    fake = _fake_httpx_client_factory(
        monkeypatch,
        [_FakeResponse({"data": [{"id": "model-a-free"}, {"id": "model-b-free"}]})],
    )
    targets = [("cid-1", "https://api.example.com/v1", "key-1")]

    first = sync.fetch_opencode_free_models_for_providers(targets)
    second = sync.fetch_opencode_free_models_for_providers(targets)

    assert first == {"cid-1": ["model-a-free", "model-b-free"]}
    assert second == {"cid-1": ["model-a-free", "model-b-free"]}
    # 只发了一次网络请求（第二次命中缓存）
    assert len(fake.get_calls) == 1


def test_fetch_opencode_free_models_cache_expired_refetch(monkeypatch):
    """缓存过期后再次调用会重新发起网络请求。"""
    fake = _fake_httpx_client_factory(
        monkeypatch,
        [
            _FakeResponse({"data": [{"id": "model-a-free"}]}),
            _FakeResponse({"data": [{"id": "model-b-free"}]}),
        ],
    )
    targets = [("cid-2", "https://api.example.com/v1", "key-2")]

    sync.fetch_opencode_free_models_for_providers(targets)
    # 人为让缓存过期
    cache_key = ("cid-2", "https://api.example.com/v1", "key-2")
    with sync._OPENCODE_FREE_CACHE_LOCK:
        old_ts, models = sync._OPENCODE_FREE_CACHE[cache_key]
        sync._OPENCODE_FREE_CACHE[cache_key] = (old_ts - sync._OPENCODE_FREE_CACHE_TTL - 1, models)

    result = sync.fetch_opencode_free_models_for_providers(targets)
    assert result == {"cid-2": ["model-b-free"]}
    assert len(fake.get_calls) == 2


def test_fetch_opencode_free_models_inflight_dedup(monkeypatch):
    """并发调用同一实例只发一次网络请求，其余等待同一结果。"""
    fake = _FakeClient([_FakeResponse({"data": [{"id": "model-a-free"}]})])
    started = threading.Event()
    release = threading.Event()

    original_get = fake.get

    def _slow_get(url, headers=None):
        started.set()
        release.wait(timeout=5)
        return original_get(url, headers)

    fake.get = _slow_get

    class _FakeModule:
        Client = lambda *a, **kw: fake
        Timeout = lambda *a, **kw: kw.get("timeout", a[0] if a else None)

    monkeypatch.setitem(sys.modules, "httpx", _FakeModule())

    targets = [("cid-3", "https://api.example.com/v1", "key-3")]
    results: Dict[str, Dict] = {}

    def _run(name: str):
        results[name] = sync.fetch_opencode_free_models_for_providers(targets)

    t1 = threading.Thread(target=_run, args=("t1",))
    t2 = threading.Thread(target=_run, args=("t2",))
    t1.start()
    assert started.wait(timeout=5)  # t1 已进入网络请求
    t2.start()
    time.sleep(0.3)  # 给 t2 时间到达 in-flight 检查点
    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    # 两个调用都拿到结果，但只发了一次网络请求
    assert results["t1"] == {"cid-3": ["model-a-free"]}
    assert results["t2"] == {"cid-3": ["model-a-free"]}
    assert len(fake.get_calls) == 1


def test_fetch_opencode_free_models_different_instances_independent(monkeypatch):
    """不同实例互不影响：各自独立拉取。"""
    fake = _fake_httpx_client_factory(
        monkeypatch,
        [
            _FakeResponse({"data": [{"id": "model-a-free"}]}),
            _FakeResponse({"data": [{"id": "model-b-free"}]}),
        ],
    )
    targets = [
        ("cid-a", "https://api-a.example.com/v1", "key-a"),
        ("cid-b", "https://api-b.example.com/v1", "key-b"),
    ]
    result = sync.fetch_opencode_free_models_for_providers(targets)
    assert result == {"cid-a": ["model-a-free"], "cid-b": ["model-b-free"]}
    assert len(fake.get_calls) == 2


def test_fetch_opencode_free_models_failure_not_cached(monkeypatch):
    """网络失败不写缓存：下一次调用会重试。"""
    fake = _FakeClient([RuntimeError("network down")])

    class _FakeModule:
        Client = lambda *a, **kw: fake
        Timeout = lambda *a, **kw: kw.get("timeout", a[0] if a else None)

    monkeypatch.setitem(sys.modules, "httpx", _FakeModule())

    targets = [("cid-4", "https://api.example.com/v1", "key-4")]
    assert sync.fetch_opencode_free_models_for_providers(targets) == {}
    cache_key = ("cid-4", "https://api.example.com/v1", "key-4")
    with sync._OPENCODE_FREE_CACHE_LOCK:
        assert cache_key not in sync._OPENCODE_FREE_CACHE
        assert cache_key not in sync._OPENCODE_FREE_INFLIGHT


def test_fetch_opencode_free_models_key_includes_instance_params(monkeypatch):
    """P1-1：同 cid 修改 API_URL/API_KEY 后不命中旧缓存，重新拉取。"""
    fake = _fake_httpx_client_factory(
        monkeypatch,
        [
            _FakeResponse({"data": [{"id": "model-old-free"}]}),
            _FakeResponse({"data": [{"id": "model-new-free"}]}),
        ],
    )
    targets_old = [("cid-x", "https://old.example.com/v1", "key-old")]
    targets_new = [("cid-x", "https://new.example.com/v1", "key-new")]

    first = sync.fetch_opencode_free_models_for_providers(targets_old)
    second = sync.fetch_opencode_free_models_for_providers(targets_new)

    assert first == {"cid-x": ["model-old-free"]}
    # 同 cid 不同实例参数：重新发请求，拿到新实例结果
    assert second == {"cid-x": ["model-new-free"]}
    assert len(fake.get_calls) == 2


def test_fetch_opencode_free_models_exception_cleans_inflight(monkeypatch):
    """P1-2：owner 请求抛异常时仍清理 in-flight，不残留导致后续永久等待。"""
    fake = _FakeClient([RuntimeError("boom")])

    class _FakeModule:
        Client = lambda *a, **kw: fake
        Timeout = lambda *a, **kw: kw.get("timeout", a[0] if a else None)

    monkeypatch.setitem(sys.modules, "httpx", _FakeModule())

    targets = [("cid-5", "https://api.example.com/v1", "key-5")]
    # 第一次：请求异常 → 不写缓存，但 in-flight 必须清干净
    assert sync.fetch_opencode_free_models_for_providers(targets) == {}
    cache_key = ("cid-5", "https://api.example.com/v1", "key-5")
    with sync._OPENCODE_FREE_CACHE_LOCK:
        assert cache_key not in sync._OPENCODE_FREE_INFLIGHT
        assert cache_key not in sync._OPENCODE_FREE_CACHE
