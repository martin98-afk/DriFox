# -*- coding: utf-8 -*-
"""ContextPipeline cascade 行为测试。

覆盖 spec §4.6 三条规则 + §4.8 注册表规则：
  空链 / order 排序 / 同 order 覆盖 / stage 过滤 / 达标即停 / 分层熔断 / 异常隔离 / 预算替换
"""

import pytest

from app.core.context.pipeline import ContextPipeline
from app.core.context.view import ContextView
from app.plugins.contracts.context_policy import (
    CACHE_NONE,
    STAGE_INGEST,
    STAGE_SEND,
    STAGE_UI,
    BudgetResult,
    TierOutcome,
)
from app.plugins.registries.context_policy_registry import ContextPolicyRegistry


class _FakeTier:
    """可编排的假 tier：记录调用次数，按配置产出 messages / 抛错。"""

    def __init__(self, tier_id, order, stages=None, saved=0, new_len=None, raises=False, label=""):
        self.id = tier_id
        self.label = label or tier_id
        self.order = order
        self.stages = frozenset(stages or {STAGE_SEND})
        self.cache_impact = CACHE_NONE
        self.saved = saved
        self.new_len = new_len
        self.raises = raises
        self.calls = 0

    def should_apply(self, view):
        return True

    def apply(self, view):
        self.calls += 1
        if self.raises:
            raise RuntimeError("boom")
        msgs = view.messages
        if self.new_len is not None:
            msgs = msgs[: self.new_len]
        return TierOutcome(messages=msgs, saved_tokens=self.saved, note="x")


@pytest.fixture
def registry():
    reg = ContextPolicyRegistry()
    yield reg
    reg.unregister_source("test")


def _view(n=100, stage=STAGE_SEND, target=10, budget=1000):
    return ContextView(
        messages=[{"role": "user", "content": "m" * 50} for _ in range(n)],
        budget=budget,
        target_tokens=target,
        llm_config={"模型名称": "gpt-4o", "上下文长度": 128000},
        stage=stage,
    )


def test_empty_chain_returns_unchanged(registry):
    v = ContextPipeline(registry).run(_view())
    assert len(v.messages) == 100
    assert v.stats == []


def test_chain_sorted_by_order(registry):
    b = _FakeTier("b", 30)
    a = _FakeTier("a", 10)
    registry.register_tier(b, "test")
    registry.register_tier(a, "test")
    assert [t.id for t in registry.resolve_chain(STAGE_SEND)] == ["a", "b"]


def test_same_order_later_wins(registry):
    first = _FakeTier("dup", 10, label="first")
    second = _FakeTier("dup", 10, label="second")
    registry.register_tier(first, "test")
    registry.register_tier(second, "test")
    assert registry.resolve_chain(STAGE_SEND)[0].label == "second"


def test_stage_filter_excludes_ingest_only(registry):
    registry.register_tier(_FakeTier("offload", 40, stages={STAGE_INGEST}), "test")
    registry.register_tier(_FakeTier("prune", 30, stages={STAGE_SEND, STAGE_UI}), "test")
    assert [t.id for t in registry.resolve_chain(STAGE_UI)] == ["prune"]
    assert [t.id for t in registry.resolve_chain(STAGE_INGEST)] == ["offload"]


def test_stops_when_target_reached(registry):
    """达标即停：一层跑完 used <= target → 后续层不再执行"""
    light = _FakeTier("light", 10, saved=500, new_len=2)
    heavy = _FakeTier("heavy", 80, saved=1)
    registry.register_tier(light, "test")
    registry.register_tier(heavy, "test")
    # target 极大 → light 产出 2 条后即达标 → heavy 不跑
    ContextPipeline(registry).run(_view(target=10**9))
    assert light.calls == 1
    assert heavy.calls == 0


def test_continues_when_target_not_reached(registry):
    """未达标则继续跑后续层"""
    light = _FakeTier("light", 10, saved=1, new_len=50)
    heavy = _FakeTier("heavy", 80, saved=1)
    registry.register_tier(light, "test")
    registry.register_tier(heavy, "test")
    # target=1 永不达标 → 两层都跑
    ContextPipeline(registry).run(_view(n=100, target=1))
    assert light.calls == 1 and heavy.calls == 1


def test_first_tier_always_runs(registry):
    """cascade 至少跑第一层（达标判定在层执行之后）"""
    heavy = _FakeTier("heavy", 80)
    registry.register_tier(heavy, "test")
    ContextPipeline(registry).run(_view(n=2, target=10**9))
    assert heavy.calls == 1


def test_circuit_break_after_repeated_no_gain(registry):
    """连续 2 次无收益 → 熔断本层，本轮后续层不再跑"""
    noop = _FakeTier("noop", 10, saved=0)
    after = _FakeTier("after", 20, saved=0)
    registry.register_tier(noop, "test")
    registry.register_tier(after, "test")
    v = _view(n=100, target=1)  # 永不达标
    v.failure_counts["noop"] = 1  # 已是第 1 次
    v = ContextPipeline(registry).run(v)
    assert v.failure_counts.get("noop") == 2
    assert after.calls == 0  # 熔断后 break


def test_exception_isolated_and_recorded(registry):
    bad = _FakeTier("bad", 10, raises=True)
    good = _FakeTier("good", 20, saved=5)
    registry.register_tier(bad, "test")
    registry.register_tier(good, "test")
    v = ContextPipeline(registry).run(_view(target=1))
    assert good.calls == 1
    skipped = [s for s in v.stats if s.skipped]
    assert len(skipped) == 1 and skipped[0].tier_id == "bad"


def test_trigger_exception_skips_tier(registry):
    class _BadTrigger(_FakeTier):
        def should_apply(self, view):
            raise RuntimeError("trigger boom")

    bad = _BadTrigger("badtrigger", 10)
    good = _FakeTier("good", 20, saved=5)
    registry.register_tier(bad, "test")
    registry.register_tier(good, "test")
    ContextPipeline(registry).run(_view(target=1))
    assert bad.calls == 0 and good.calls == 1


def test_custom_budget_resolver(registry):
    class _R:
        id = "custom"

        def resolve(self, llm_config, system_content=""):
            return BudgetResult(budget=777, target_tokens=111)

    registry.register_budget_resolver(_R(), "test")
    v = ContextPipeline(registry).project_for_ui([{"role": "user", "content": "hi"}], {})
    assert v.budget == 777 and v.target_tokens == 111


def test_register_dispatch_by_shape(registry):
    """loader 代理只调 registry.register：按形状分派到 tier / resolver"""
    tier = _FakeTier("t1", 10)

    class _R:
        id = "r1"

        def resolve(self, llm_config, system_content=""):
            return BudgetResult(budget=1, target_tokens=1)

    registry.register(tier, "test")
    registry.register(_R(), "test")
    assert "t1" in registry.tiers()
    assert registry.get_budget_resolver().id == "r1"


def test_prune_precedes_offload_in_ingest():
    """ingest stage 里 tool_prune 必须先于 tool_offload。

    落盘判定基于内容长度，若先落盘再截断，阈值会基于未截断内容失真。
    """
    from app.plugins.loaders.runtime_component_loader import _make_context_tier_loader

    _make_context_tier_loader().scan_roots()
    reg = ContextPolicyRegistry.get_instance()
    ids = [t.id for t in reg.resolve_chain(STAGE_INGEST)]
    if "tool_prune" in ids and "tool_offload" in ids:
        assert ids.index("tool_prune") < ids.index("tool_offload")


def test_system_plugin_tiers_are_prefix_safe():
    """system-context 提供的层必须全部 cache_impact ≠ invalidate。

    系统插件只承载「每轮投影产出字节恒定」的层（入口截断 + 冻结预览）；
    任何改写历史消息的层（图片剥离/去重/摘要/尾保留/LLM 摘要）会破坏
    prompt cache 前缀，不得进入系统插件。
    """
    from app.plugins.loaders.runtime_component_loader import _make_context_tier_loader

    _make_context_tier_loader().scan_roots()
    reg = ContextPolicyRegistry.get_instance()
    for tier in reg.tiers().values():
        assert tier.cache_impact != "invalidate", f"系统插件的 tier '{tier.id}' 会破坏前缀"
        assert hasattr(tier, "order") and hasattr(tier, "stages")


def test_view_token_cache_invalidated_on_replace():
    v = _view(n=10)
    first = v.used_tokens
    v.replace_messages([{"role": "user", "content": "z" * 5000}])
    assert v.used_tokens != first
