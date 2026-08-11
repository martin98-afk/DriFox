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
    """reasoning=True 但 reasoning_options=[] → 思考不可控，不显示思考开关。

    思考开关 ≠ 会思考：models.dev 未给出控制方式时未知，保守不误报。
    """
    info = {
        "modalities": {"input": ["text"], "output": ["text"]},
        "limit": {"context": 200000},
        "reasoning": True,
        "reasoning_options": [],
    }
    result = sync._transform_model("opencode", "some-reasoning-model", info)
    assert result["supports_thinking"] is False
    assert "thinking_param" not in result


def test_transform_model_reasoning_options_empty_type_no_controls():
    """reasoning_options 存在但 type 缺失/为空 → 控制方式未知，不显示思考开关。"""
    info = {
        "modalities": {"input": ["text"], "output": ["text"]},
        "limit": {"context": 200000},
        "reasoning": True,
        "reasoning_options": [{"type": ""}],
    }
    result = sync._transform_model("opencode", "some-reasoning-model", info)
    assert result["supports_thinking"] is False
    assert "thinking_param" not in result


def test_transform_model_reasoning_with_toggle_controls():
    """reasoning_options 明确 type=toggle → 支持思考开关（thinking）。"""
    info = {
        "modalities": {"input": ["text"], "output": ["text"]},
        "limit": {"context": 200000},
        "reasoning": True,
        "reasoning_options": [{"type": "toggle"}],
    }
    result = sync._transform_model("opencode", "some-reasoning-model", info)
    assert result["supports_thinking"] is True
    assert result["thinking_param"] == "thinking"


def test_transform_model_reasoning_effort_budget_controls():
    """effort / budget_tokens 也算可控思考，标记支持思考开关。"""
    info_template = {
        "modalities": {"input": ["text"], "output": ["text"]},
        "limit": {"context": 200000},
        "reasoning": True,
    }
    for opt, expected in ({"type": "effort"}, "reasoning_effort"), ({"type": "budget_tokens"}, "thinking_budget"):
        info = {**info_template, "reasoning_options": [opt]}
        result = sync._transform_model("opencode", "some-reasoning-model", info)
        assert result["supports_thinking"] is True
        assert result["thinking_param"] == expected


def test_transform_model_cost():
    """cost 四字段完整解析，原样保留不换算。"""
    info = {
        "modalities": {"input": ["text"], "output": ["text"]},
        "limit": {"context": 128000},
        "cost": {"input": 0.6, "output": 3, "cache_read": 0.1, "cache_write": 0},
    }
    result = sync._transform_model("zhipuai", "glm-test", info)
    assert result["cost"] == {"input": 0.6, "output": 3, "cache_read": 0.1, "cache_write": 0}


def test_transform_model_cost_missing():
    """models.dev 未提供 cost → 各字段为 None，不报错。"""
    info = {
        "modalities": {"input": ["text"], "output": ["text"]},
        "limit": {"context": 128000},
    }
    result = sync._transform_model("openai", "gpt-test", info)
    assert result["cost"] == {"input": None, "output": None, "cache_read": None, "cache_write": None}


def test_transform_model_cost_partial():
    """cost 部分字段缺失 → 缺失位为 None。"""
    info = {
        "modalities": {"input": ["text"], "output": ["text"]},
        "limit": {"context": 128000},
        "cost": {"input": 1, "output": 3.2},
    }
    result = sync._transform_model("openai", "gpt-test", info)
    assert result["cost"]["input"] == 1
    assert result["cost"]["output"] == 3.2
    assert result["cost"]["cache_read"] is None
    assert result["cost"]["cache_write"] is None


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
    # opencode-test: reasoning=True 但 reasoning_options=[] → 无控制方式，不支持思考开关
    assert caps["opencode-test"]["supports_thinking"] is False
    assert "thinking_param" not in caps["opencode-test"]


# ============================================================
# _merge_model_caps（跨 provider 同名合并）
# ============================================================
def test_merge_model_caps_thinking_optimistic():
    """同名模型：后 provider 无思考不降级前 provider 的思考支持。"""
    existing = {"supports_thinking": True, "thinking_param": "thinking", "context_limit": 262144}
    new = {"supports_thinking": False, "context_limit": 131072}
    merged = sync._merge_model_caps(existing, new)
    assert merged["supports_thinking"] is True
    # new 无 thinking_param → 保留 existing
    assert merged["thinking_param"] == "thinking"
    # new 有 context_limit → 取 new
    assert merged["context_limit"] == 131072


def test_merge_model_caps_reverse_order_still_optimistic():
    """反向：前 provider 无思考、后 provider 有思考 → 合并后仍支持思考。"""
    existing = {"supports_thinking": False, "context_limit": 131072}
    new = {"supports_thinking": True, "thinking_param": "reasoning_effort"}
    merged = sync._merge_model_caps(existing, new)
    assert merged["supports_thinking"] is True
    assert merged["thinking_param"] == "reasoning_effort"


def test_merge_model_caps_cost_field_merge():
    """cost 字段级合并：new 非 None 覆盖，None 保留 existing。"""
    existing = {"cost": {"input": 0.6, "output": 3, "cache_read": 0.1, "cache_write": None}}
    new = {"cost": {"input": 0.8, "output": None, "cache_read": None, "cache_write": 0.5}}
    merged = sync._merge_model_caps(existing, new)
    assert merged["cost"]["input"] == 0.8
    assert merged["cost"]["output"] == 3
    assert merged["cost"]["cache_read"] == 0.1
    assert merged["cost"]["cache_write"] == 0.5


# ============================================================
# get_model_capabilities（动态为准：models.dev 有数据则硬编码不参与思考判定）
# ============================================================
def test_get_model_capabilities_dynamic_false_overrides_hardcode(monkeypatch):
    """动态 supports_thinking=False 时，硬编码 True 不再拉回（models.dev 完全为准）。

    如 MiniMax-M2.7：硬编码标 supports_thinking=True，但 models.dev 无
    reasoning_options → 动态 False → 最终 False（思考开关不显示）。
    """
    from app.core import model_capabilities as mc

    dynamic = sync.DynamicModelsResult(
        provider_models={},
        model_capabilities={"kimi-k2.5": {"supports_thinking": False, "context_limit": 999}},
        from_cache=False,
        fetched_at=None,
    )
    monkeypatch.setattr(sync, "get_dynamic_models", lambda: dynamic)
    result = mc.get_model_capabilities("kimi-k2.5")
    assert result["supports_thinking"] is False  # 动态为准，硬编码不参与
    assert result["context_limit"] == 999  # 非思考字段动态优先


def test_get_model_capabilities_dynamic_false_when_no_hardcode(monkeypatch):
    """硬编码无记录 + 动态 False → False。"""
    from app.core import model_capabilities as mc

    dynamic = sync.DynamicModelsResult(
        provider_models={},
        model_capabilities={"brand-new-model": {"supports_thinking": False, "context_limit": 12345}},
        from_cache=False,
        fetched_at=None,
    )
    monkeypatch.setattr(sync, "get_dynamic_models", lambda: dynamic)
    result = mc.get_model_capabilities("brand-new-model")
    assert result["supports_thinking"] is False
    assert result["context_limit"] == 12345


def test_get_model_capabilities_dynamic_missing_uses_hardcode(monkeypatch):
    """models.dev 查不到该模型（动态 None）→ 硬编码兜底生效。"""
    from app.core import model_capabilities as mc

    dynamic = sync.DynamicModelsResult(provider_models={}, model_capabilities={}, from_cache=False, fetched_at=None)
    monkeypatch.setattr(sync, "get_dynamic_models", lambda: dynamic)
    result = mc.get_model_capabilities("glm-4-flash")
    assert result["supports_thinking"] is True  # 硬编码兜底
    assert result["thinking_param"] == "thinking"


def test_get_model_capabilities_dynamic_thinking_fields_replace_hardcode(monkeypatch):
    """动态有思考字段时，硬编码的 thinking_param 残留不混入（思考字段整体以动态为准）。"""
    from app.core import model_capabilities as mc

    dynamic = sync.DynamicModelsResult(
        provider_models={},
        # 动态只有 supports_thinking=False，无 thinking_param → 硬编码的
        # thinking_param="thinking" 不应混入结果造成矛盾状态
        model_capabilities={"minimax-m2.5": {"supports_thinking": False, "context_limit": 512000}},
        from_cache=False,
        fetched_at=None,
    )
    monkeypatch.setattr(sync, "get_dynamic_models", lambda: dynamic)
    result = mc.get_model_capabilities("minimax-m2.5")
    assert result["supports_thinking"] is False
    assert "thinking_param" not in result


# ============================================================
# cache helpers
# ============================================================
def test_is_cache_valid(tmp_path: Path):
    valid = {"_cached_at": time.time(), "_schema_version": sync.CACHE_SCHEMA_VERSION}
    invalid = {"_cached_at": time.time() - sync.CACHE_TTL_SECONDS - 1, "_schema_version": sync.CACHE_SCHEMA_VERSION}
    missing = {}
    assert sync._is_cache_valid(valid) is True
    assert sync._is_cache_valid(invalid) is False
    assert sync._is_cache_valid(missing) is False
    assert sync._is_cache_valid(None) is False


def test_is_cache_valid_schema_mismatch(tmp_path: Path):
    """schema 版本不匹配 → 缓存无效，触发重拉（让旧缓存带上新字段）。"""
    old = {"_cached_at": time.time(), "_schema_version": 1}
    assert sync._is_cache_valid(old) is False


def test_save_and_load_cache(tmp_path: Path):
    path = tmp_path / "cache.json"
    data = {
        "_cached_at": time.time(),
        "_schema_version": sync.CACHE_SCHEMA_VERSION,
        "provider_models": {},
        "model_capabilities": {},
    }
    sync._save_cache(data, path)
    loaded = sync._load_cache(path)
    assert loaded is not None
    assert loaded["_cached_at"] == data["_cached_at"]
    assert loaded["_schema_version"] == sync.CACHE_SCHEMA_VERSION


# ============================================================
# load_dynamic_models
# ============================================================
def test_load_dynamic_models_uses_cache_when_valid(monkeypatch, tmp_path: Path):
    cache = {
        "_cached_at": time.time(),
        "_schema_version": sync.CACHE_SCHEMA_VERSION,
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


# ============================================================
# get_dynamic_models（主线程安全：永不发网络）+ refresh_dynamic_models_async
# ============================================================
@pytest.fixture(autouse=True)
def _clean_memory_cache():
    """清理模块级内存缓存与 in-flight 标记，避免测试间互相污染。"""
    sync.clear_memory_cache()
    with sync._REFRESH_LOCK:
        sync._REFRESH_INFLIGHT = None
    yield
    sync.clear_memory_cache()
    with sync._REFRESH_LOCK:
        sync._REFRESH_INFLIGHT = None


def test_get_dynamic_models_never_fetches_network(monkeypatch, tmp_path: Path):
    """get_dynamic_models 在缓存缺失/过期时也绝不发网络请求（主线程安全）。"""
    fetch_called = []

    def _fake_fetch():
        fetch_called.append(True)
        raise AssertionError("get_dynamic_models 不应发起网络")

    monkeypatch.setattr(sync, "_fetch_remote", _fake_fetch)
    # 无缓存文件 + 内存缓存为空 → get_dynamic_models 应返回空而非触发网络
    monkeypatch.setattr(sync, "_get_cache_path", lambda: tmp_path / "none.json")
    result = sync.get_dynamic_models()
    assert fetch_called == []
    assert result.provider_models == {}
    assert result.model_capabilities == {}


def test_get_dynamic_models_reads_valid_file_cache(monkeypatch, tmp_path: Path):
    """内存缓存为空但文件缓存有效时，get_dynamic_models 读文件并填充内存缓存。"""
    cache = {
        "_cached_at": time.time(),
        "_schema_version": sync.CACHE_SCHEMA_VERSION,
        "provider_models": {"OpenAI": ["gpt-file-cached"]},
        "model_capabilities": {"gpt-file-cached": {"context_limit": 123}},
    }
    path = tmp_path / "cache.json"
    sync._save_cache(cache, path)
    monkeypatch.setattr(sync, "_get_cache_path", lambda: path)
    monkeypatch.setattr(sync, "_fetch_remote", lambda: (_ for _ in ()).throw(AssertionError("不应网络")))

    result = sync.get_dynamic_models()
    assert result.provider_models["OpenAI"] == ["gpt-file-cached"]
    # 第二次调用命中内存缓存，不再读文件
    monkeypatch.setattr(sync, "_load_cache", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不应读文件")))
    result2 = sync.get_dynamic_models()
    assert result2.provider_models["OpenAI"] == ["gpt-file-cached"]


def test_refresh_dynamic_models_async_fills_memory_cache(monkeypatch, tmp_path: Path):
    """后台刷新成功后自动填充 get_dynamic_models 的内存缓存（至少有一次成功结果）。"""
    remote_data = {
        "openai": {
            "models": {
                "gpt-refreshed": {
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 128000},
                    "reasoning": False,
                }
            }
        }
    }
    monkeypatch.setattr(sync, "_fetch_remote", lambda: remote_data)
    monkeypatch.setattr(sync, "_fetch_opencode_zen_free_models", lambda **kw: ([], {}))
    monkeypatch.setattr(sync, "_get_cache_path", lambda: tmp_path / "cache.json")

    done = threading.Event()
    seen = {}

    def _on_done(result):
        seen["result"] = result
        done.set()

    started = sync.refresh_dynamic_models_async(on_done=_on_done)
    assert started is True
    assert done.wait(timeout=5), "后台刷新未在 5s 内完成"
    # on_done 收到成功结果
    assert "gpt-refreshed" in seen["result"].provider_models["OpenAI"]
    # 内存缓存已填充：后续 get_dynamic_models 直接命中
    assert "gpt-refreshed" in sync.get_dynamic_models().provider_models["OpenAI"]


def test_refresh_dynamic_models_async_singleton_dedup(monkeypatch, tmp_path: Path):
    """并发触发多次后台刷新只发一路网络请求（单飞去重）。"""
    call_count = []

    def _slow_fetch():
        call_count.append(True)
        time.sleep(0.3)
        return {}

    monkeypatch.setattr(sync, "_fetch_remote", _slow_fetch)
    monkeypatch.setattr(sync, "_fetch_opencode_zen_free_models", lambda **kw: ([], {}))
    monkeypatch.setattr(sync, "_get_cache_path", lambda: tmp_path / "cache.json")

    started1 = sync.refresh_dynamic_models_async()
    started2 = sync.refresh_dynamic_models_async()  # in-flight 期间 → 忽略
    assert started1 is True
    assert started2 is False
    # 等第一个完成，确认只发了一路网络
    time.sleep(0.8)
    assert len(call_count) == 1
    with sync._REFRESH_LOCK:
        assert sync._REFRESH_INFLIGHT is None  # in-flight 已清理
    # 完成后可再次触发
    started3 = sync.refresh_dynamic_models_async()
    assert started3 is True
    # 等第三次完成，避免后台线程在 teardown 清理状态时仍在运行
    time.sleep(0.8)
