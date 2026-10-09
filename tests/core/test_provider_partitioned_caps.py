# -*- coding: utf-8 -*-
"""P1-22 批A 回归：models.dev provider 维度分区精确查

背景
----
models.dev 的扁平索引按 model_id 索引，同名模型跨服务商（kimi-k2.5 在 moonshotai /
opencode-go）会走 `_merge_model_caps` 合并，导致「A 家模型被 B 家元数据抬升」——
这是设计文档 §2.4 记录的 13 例串味缺陷之一（如 space-bunny 524288 抬升）。

修法：`_parse_models_dev_data` 额外产出 provider 维度的**嵌套分区索引**
（provider_id → model_id → caps，不合并），`get_model_capabilities(model, provider=)`
先查分区，未命中降级扁平索引（老缓存兼容）。

本组锁定：
1. 分区精确查：同名模型在两个 provider 下拿到各自不同的 caps
2. 降级：无 provider / provider 未命中 / 老缓存无分区键 → 走扁平索引
3. 老缓存兼容：缓存缺 model_capabilities_partitioned 键时 get 默认空 dict
"""

import json
import time
from pathlib import Path

import pytest

from app.core.modelmeta import model_capabilities as mc
from app.core.modelmeta import models_dev_sync as sync


@pytest.fixture()
def dynamic_with_partition(monkeypatch):
    """构造带分区索引的 DynamicModelsResult（kimi-k2.5 在两 provider 下 caps 不同）"""
    flat = {
        # 扁平索引（合并结果）：被"更支持"的一方抬升
        "kimi-k2.5": {"context_limit": 262144, "max_output_tokens": 524288, "source": "models.dev"},
    }
    partitioned = {
        "moonshotai": {
            "kimi-k2.5": {"context_limit": 262144, "max_output_tokens": 8192, "source": "models.dev"},
        },
        "opencode-go": {
            "kimi-k2.5": {"context_limit": 262144, "max_output_tokens": 524288, "source": "models.dev"},
        },
    }
    dynamic = sync.DynamicModelsResult(
        provider_models={},
        model_capabilities=flat,
        model_capabilities_partitioned=partitioned,
        from_cache=False,
        fetched_at=None,
    )
    monkeypatch.setattr(sync, "get_dynamic_models", lambda: dynamic)
    # 服务商名 → provider_id 映射（模拟插件 models_dev_id 声明）
    monkeypatch.setattr(mc, "_get_models_dev_map", lambda: {"Kimi 官方": "moonshotai", "OpenCode Go": "opencode-go"})
    return dynamic


class TestPartitionedLookup:
    def test_same_model_two_providers_get_distinct_caps(self, dynamic_with_partition):
        """同名模型在不同 provider 下拿到各自分区的 caps（不串味）"""
        official = mc.get_model_capabilities("kimi-k2.5", "Kimi 官方")
        zen = mc.get_model_capabilities("kimi-k2.5", "OpenCode Go")

        assert official.get("max_output_tokens") == 8192, "Kimi 官方分区值"
        assert zen.get("max_output_tokens") == 524288, "OpenCode Go 分区值"
        assert official.get("max_output_tokens") != zen.get("max_output_tokens"), "两分区必须不同"

    def test_no_provider_uses_flat_index(self, dynamic_with_partition):
        """不传 provider → 走扁平索引（既有 15 处调用零改动，行为不变）"""
        result = mc.get_model_capabilities("kimi-k2.5")
        assert result.get("max_output_tokens") == 524288, "扁平索引是合并后的值"

    def test_unknown_provider_falls_back_to_flat(self, dynamic_with_partition):
        """provider 不在 models_dev 映射里 → 降级扁平索引"""
        result = mc.get_model_capabilities("kimi-k2.5", "某个自定义服务商")
        assert result.get("max_output_tokens") == 524288

    def test_provider_mapped_but_model_absent_falls_back_to_flat(self, dynamic_with_partition):
        """provider 有映射但该模型不在其分区 → 降级扁平索引"""
        result = mc.get_model_capabilities("不存在的模型", "Kimi 官方")
        assert result == {}, "两边都没有 → 空 dict"


class TestCacheBackwardCompat:
    def test_old_cache_without_partition_key_yields_empty(self, monkeypatch, tmp_path: Path):
        """老缓存（无 model_capabilities_partitioned 键）→ get 默认空 dict，不报错"""
        stale = {
            "_cached_at": time.time() - sync.CACHE_TTL_SECONDS - 1,
            "provider_models": {"OpenAI": ["gpt-old"]},
            "model_capabilities": {"gpt-old": {"context_limit": 128000}},
        }
        path = tmp_path / "cache.json"
        sync._save_cache(stale, path)
        # _fetch_remote 约定返回 (data, url) 二元组；data=None 表示拉取失败
        monkeypatch.setattr(sync, "_fetch_remote", lambda: (None, ""))

        result = sync.load_dynamic_models(cache_path=path)
        assert result.provider_models["OpenAI"] == ["gpt-old"]
        assert result.model_capabilities_partitioned == {}, "老缓存缺分区键 → 空 dict 降级"
        assert result.from_cache is True

    def test_new_cache_roundtrips_partition(self, monkeypatch, tmp_path: Path):
        """新缓存写入分区键后可完整读回"""
        cache = {
            "_cached_at": time.time(),
            "_url": "x",
            "_schema_version": sync.CACHE_SCHEMA_VERSION,
            "_content_version": sync.CACHE_CONTENT_VERSION,
            "provider_models": {"OpenAI": ["gpt-new"]},
            "model_capabilities": {"gpt-new": {"context_limit": 1}},
            "model_capabilities_partitioned": {"openai": {"gpt-new": {"context_limit": 1}}},
        }
        sync._save_cache(cache, tmp_path / "cache.json")
        monkeypatch.setattr(sync, "_fetch_remote", lambda: (_ for _ in ()).throw(AssertionError("不应走网络")))

        result = sync.load_dynamic_models(cache_path=tmp_path / "cache.json")
        assert result.model_capabilities_partitioned["openai"]["gpt-new"]["context_limit"] == 1
        assert result.from_cache is True

    def test_parse_returns_partition_without_merging(self, monkeypatch):
        """解析层：分区索引保留各 provider 原值（不合并），扁平索引才合并"""
        monkeypatch.setattr(
            sync,
            "_get_models_dev_map",
            lambda: {"Kimi 官方": "moonshotai", "OpenCode Go": "opencode-go"},
        )
        data = {
            "moonshotai": {
                "models": {
                    "kimi-k2.5": {
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "limit": {"context": 262144, "output": 8192},
                        "reasoning": False,
                    }
                }
            },
            "opencode-go": {
                "models": {
                    "kimi-k2.5": {
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "limit": {"context": 262144, "output": 524288},
                        "reasoning": False,
                    }
                }
            },
        }
        provider_models, flat, partitioned = sync._parse_models_dev_data(data)

        # 分区：各保留原值
        assert partitioned["moonshotai"]["kimi-k2.5"]["max_output_tokens"] == 8192
        assert partitioned["opencode-go"]["kimi-k2.5"]["max_output_tokens"] == 524288
        # 扁平：合并（后到者/更支持者胜出）
        assert flat["kimi-k2.5"]["max_output_tokens"] == 524288
        # provider_models 两家都收到该模型
        assert "kimi-k2.5" in provider_models["Kimi 官方"]
        assert "kimi-k2.5" in provider_models["OpenCode Go"]


class TestPartitionedHelperRobustness:
    def test_empty_partition_returns_none(self, monkeypatch):
        """分区为空（老缓存）→ 辅助函数返回 None，调用方降级"""
        dynamic = sync.DynamicModelsResult(
            provider_models={}, model_capabilities={}, model_capabilities_partitioned={}, from_cache=True
        )
        monkeypatch.setattr(sync, "get_dynamic_models", lambda: dynamic)
        assert mc._get_partitioned_model_capabilities("m", "P") is None

    def test_missing_dynamic_attribute_returns_none(self, monkeypatch):
        """动态结果对象缺 partition 属性（旧对象）→ 返回 None 不抛异常"""

        class _Legacy:
            provider_models: dict = {}
            model_capabilities: dict = {}

        monkeypatch.setattr(sync, "get_dynamic_models", lambda: _Legacy())
        assert mc._get_partitioned_model_capabilities("m", "P") is None

    def test_lowercase_model_fallback_in_partition(self, dynamic_with_partition, monkeypatch):
        """分区内模型名大小写不同也能命中（小写回退，与扁平索引同款）"""
        monkeypatch.setitem(dynamic_with_partition.model_capabilities_partitioned, "moonshotai", {"Kimi-K2.5": {"max_output_tokens": 4096}})
        result = mc.get_model_capabilities("kimi-k2.5", "Kimi 官方")
        assert result.get("max_output_tokens") == 4096


class TestCacheFileShape:
    def test_saved_cache_contains_partition_key(self, monkeypatch, tmp_path: Path):
        """远程拉取成功后写入的缓存必须含分区键（供下次冷启动精确查）"""
        monkeypatch.setattr(
            sync,
            "_get_models_dev_map",
            lambda: {"OpenAI": "openai"},
        )
        remote = {
            "openai": {
                "models": {
                    "gpt-x": {
                        "modalities": {"input": ["text"], "output": ["text"]},
                        "limit": {"context": 1000, "output": 500},
                        "reasoning": False,
                    }
                }
            }
        }
        monkeypatch.setattr(sync, "_fetch_remote", lambda: (remote, "http://fake"))
        monkeypatch.setattr(sync, "_fetch_opencode_zen_free_models", lambda **kwargs: ([], {}))
        path = tmp_path / "cache.json"
        sync.load_dynamic_models(force=True, cache_path=path)

        raw = json.loads(path.read_text(encoding="utf-8"))
        assert "model_capabilities_partitioned" in raw
        assert raw["model_capabilities_partitioned"]["openai"]["gpt-x"]["max_output_tokens"] == 500
