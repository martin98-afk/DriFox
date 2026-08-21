# -*- coding: utf-8 -*-
"""CacheHitRateTracker 命中率口径回归测试

回归背景（2026-08-21）：用户反馈服务商后台显示 99% 缓存命中，软件只显示 70%+。
根因：OpenAI 兼容 provider（模型名不以 openai/azure/claude/anthropic 开头，
如 gpt-5.2 / kimi / glm / deepseek）的真实 cached_tokens 被启发式判定为「可疑」，
强制改写为估算值并虚构 cache_creation_tokens（write），聚合命中率被拉低。

口径约定（修复后）：
- OpenAI 兼容（prompt_tokens 含 cached_tokens）：
    hit_rate = cached_tokens / prompt_tokens   ← 与服务商后台口径一致
- Anthropic（input_tokens 不含缓存，三段并列）：
    hit_rate = cache_read / (cache_read + cache_creation + input_tokens)
    （input≈0 时退化为官方公式 read/(read+write)）
"""

import pytest

from app.core.workers.cache_tracker import CacheHitRateTracker


def _openai_usage(prompt: int, cached: int) -> dict:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": 500,
        "total_tokens": prompt + 500,
        "prompt_tokens_details": {"cached_tokens": cached},
    }


class TestOpenAICompatHitRate:
    """OpenAI 兼容 provider：命中率必须与 cached/prompt 对齐"""

    def test_high_hit_matches_provider_backend(self):
        """服务商后台 99% 命中（cached≈99%×prompt），软件聚合命中率应≈99%"""
        tracker = CacheHitRateTracker()
        tracker.start_session()
        # 工具会话典型形态：每轮 prompt 快速增长，每轮真实命中 99%
        prompts = [5000, 12000, 20000, 28000, 36000]
        for p in prompts:
            tracker.record_usage_dict(_openai_usage(p, int(p * 0.99)), model="gpt-5.2")

        stats = tracker.get_session_stats()
        provider_rate = sum(int(p * 0.99) for p in prompts) / sum(prompts)
        assert stats.hit_rate == pytest.approx(provider_rate, abs=0.01), (
            f"软件 {stats.hit_rate:.1%} 应与服务商口径 {provider_rate:.1%} 一致，"
            f"当前 read={stats.cache_read_tokens} "
            f"write={stats.cache_creation_5m_tokens + stats.cache_creation_1h_tokens}"
        )

    def test_no_fabricated_cache_write(self):
        """OpenAI 兼容格式无 cache_creation 字段，不得虚构 write"""
        tracker = CacheHitRateTracker()
        tracker.start_session()
        tracker.record_usage_dict(_openai_usage(20000, 19800), model="kimi-k2")
        stats = tracker.get_last_stats()
        assert stats.cache_creation_5m_tokens == 0
        assert stats.cache_creation_1h_tokens == 0

    def test_full_cache_provider_shows_100(self):
        """恒报 cached==prompt 的 provider：信任数据，显示 100%（与其后台一致）"""
        tracker = CacheHitRateTracker()
        tracker.start_session()
        for p in (10000, 15000, 20000):
            tracker.record_usage_dict(_openai_usage(p, p), model="deepseek-v4")
        assert tracker.get_session_stats().hit_rate == pytest.approx(1.0)

    def test_no_cache_shows_zero(self):
        """无缓存命中显示 0%"""
        tracker = CacheHitRateTracker()
        tracker.start_session()
        tracker.record_usage_dict(_openai_usage(10000, 0), model="glm-5")
        assert tracker.get_session_stats().hit_rate == 0.0
        assert tracker.get_session_stats().cache_hits == 0


class TestAnthropicHitRate:
    """Anthropic 格式：保留官方公式，input 计入分母"""

    def _anthropic_usage(self, input_t: int, read: int, write: int) -> dict:
        return {
            "input_tokens": input_t,
            "output_tokens": 800,
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write,
        }

    def test_official_formula_when_no_uncached_input(self):
        """input=0 时退化为官方口径 read/(read+write)"""
        tracker = CacheHitRateTracker()
        tracker.start_session()
        tracker.record_usage_dict(self._anthropic_usage(0, 90000, 1000), model="claude-sonnet-4.6")
        assert tracker.get_session_stats().hit_rate == pytest.approx(90000 / 91000, abs=1e-6)

    def test_uncached_input_counts_in_denominator(self):
        """非缓存 input 也是未命中输入，计入分母"""
        tracker = CacheHitRateTracker()
        tracker.start_session()
        tracker.record_usage_dict(self._anthropic_usage(500, 90000, 1000), model="claude-sonnet-4.6")
        assert tracker.get_session_stats().hit_rate == pytest.approx(90000 / 91500, abs=1e-6)

    def test_ephemeral_1h_write_parsed(self):
        """Anthropic 新版 cache_creation 对象结构（5m/1h）解析"""
        tracker = CacheHitRateTracker()
        tracker.start_session()
        tracker.record_usage_dict(
            {
                "input_tokens": 0,
                "output_tokens": 100,
                "cache_read_input_tokens": 50000,
                "cache_creation": {"ephemeral_5m_input_tokens": 800, "ephemeral_1h_input_tokens": 200},
            },
            model="claude-opus-4.7",
        )
        stats = tracker.get_session_stats()
        assert stats.cache_creation_5m_tokens == 800
        assert stats.cache_creation_1h_tokens == 200
        assert stats.hit_rate == pytest.approx(50000 / 51000, abs=1e-6)


class TestMixedAndAggregation:
    """聚合与混合场景"""

    def test_openai_aggregation_matches_ratio_of_sums(self):
        """聚合口径 = Σcached / Σprompt（分母分子各自求和）"""
        tracker = CacheHitRateTracker()
        tracker.start_session()
        prompts = [8000, 9000, 10000, 11000]
        for p in prompts:
            tracker.record_usage_dict(_openai_usage(p, int(p * 0.95)), model="gpt-5.2")
        expected = sum(int(p * 0.95) for p in prompts) / sum(prompts)
        assert tracker.get_session_stats().hit_rate == pytest.approx(expected, abs=0.01)

    def test_per_request_hit_rate_unchanged(self):
        """per_request_hit_rate 语义不变：命中请求数 / 总请求数"""
        tracker = CacheHitRateTracker()
        tracker.start_session()
        tracker.record_usage_dict(_openai_usage(10000, 9900), model="gpt-5.2")
        tracker.record_usage_dict(_openai_usage(10000, 0), model="gpt-5.2")
        stats = tracker.get_session_stats()
        assert stats.requests == 2
        assert stats.cache_hits == 1
        assert stats.per_request_hit_rate == pytest.approx(0.5)

    def test_zero_requests_safe(self):
        """空会话不崩溃，返回 0"""
        tracker = CacheHitRateTracker()
        tracker.start_session()
        assert tracker.get_session_stats().hit_rate == 0.0


class TestProviderHooks:
    """服务商插件钩子（ProviderDef.usage_semantics / usage_normalizer）"""

    def test_normalizer_overrides_builtin(self):
        """normalizer 优先：非标字段映射到标准口径"""
        tracker = CacheHitRateTracker()
        tracker.start_session()

        def normalizer(usage, model):
            # 非标 provider：cache_hit_tokens 是命中数，total_prompt 不含命中
            return {
                "prompt_tokens": usage.get("total_prompt", 0) + usage.get("cache_hit_tokens", 0),
                "completion_tokens": usage.get("output_cnt", 0),
                "cached_tokens": usage.get("cache_hit_tokens", 0),
                "cache_creation_5m": 0,
                "cache_creation_1h": 0,
                "input_includes_cache": True,
            }

        tracker.set_provider_hooks(normalizer=normalizer)
        tracker.record_usage_dict({"total_prompt": 200, "cache_hit_tokens": 800, "output_cnt": 50}, model="x")
        stats = tracker.get_session_stats()
        assert stats.cache_read_tokens == 800
        assert stats.hit_rate == pytest.approx(800 / 1000, abs=1e-6)

    def test_normalizer_none_falls_back(self):
        """normalizer 返回 None → 回退内置解析"""
        tracker = CacheHitRateTracker()
        tracker.start_session()
        tracker.set_provider_hooks(normalizer=lambda u, m: None)
        tracker.record_usage_dict(_openai_usage(10000, 9000), model="gpt-5.2")
        stats = tracker.get_session_stats()
        assert stats.cache_read_tokens == 9000
        assert stats.hit_rate == pytest.approx(0.9)

    def test_normalizer_exception_falls_back(self):
        """normalizer 抛异常 → 回退内置解析，不崩"""
        tracker = CacheHitRateTracker()
        tracker.start_session()

        def bad(u, m):
            raise RuntimeError("boom")

        tracker.set_provider_hooks(normalizer=bad)
        tracker.record_usage_dict(_openai_usage(10000, 9000), model="gpt-5.2")
        assert tracker.get_session_stats().hit_rate == pytest.approx(0.9)

    def test_semantics_prompt_excludes_cache(self):
        """声明 prompt_excludes_cache：OpenAI 字段名但 prompt 不含 cached"""
        tracker = CacheHitRateTracker()
        tracker.start_session()
        tracker.set_provider_hooks(semantics="prompt_excludes_cache")
        tracker.record_usage_dict(_openai_usage(1000, 9000), model="custom-v1")
        stats = tracker.get_last_stats()
        assert stats.input_includes_cache is False
        # uncached = prompt(1000) + writes(0) → hit = 9000/10000
        assert stats.hit_rate == pytest.approx(9000 / 10000, abs=1e-6)

    def test_semantics_openai_forces_flag(self):
        """声明 openai：即便字段像 Anthropic 也按含 cached 口径"""
        tracker = CacheHitRateTracker()
        tracker.start_session()
        tracker.set_provider_hooks(semantics="openai")
        tracker.record_usage_dict(
            {"input_tokens": 10000, "output_tokens": 100, "cache_read_input_tokens": 9000},
            model="custom-v2",
        )
        stats = tracker.get_last_stats()
        assert stats.input_includes_cache is True
        assert stats.hit_rate == pytest.approx(0.9)

    def test_no_hooks_behavior_unchanged(self):
        """无钩子时行为与自动检测一致（回归保护）"""
        tracker = CacheHitRateTracker()
        tracker.start_session()
        tracker.record_usage_dict(_openai_usage(10000, 9900), model="gpt-5.2")
        assert tracker.get_session_stats().hit_rate == pytest.approx(0.99)
