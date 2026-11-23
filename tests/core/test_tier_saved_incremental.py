# -*- coding: utf-8 -*-
"""[#R6] tier saved 增量差值回归：pipeline 统一 before-after 口径。

== 前置核查结论（已回报 leader）==
#I3（f8a7044a）只覆盖 view.py 的 replace_messages/_refresh_tokens_incremental
与 used_tokens property；三层 tier.apply 的 outcome.saved_tokens 走
「view.used_tokens - count_messages_tokens(result 全量)」独立路径，未覆盖。

== 修复语义 ==
pipeline.run 用 view 前后两次 #I3 增量刷新差值（O(changed)）计 saved；
三 tier 全量整表遍历删除。等价性依据：token_estimator ratio 表现行全 1.00
→ 整表 int(Σraw×ratio) 口径 == 逐条 Σper_message_tokens 口径，严格恒等。
"""

import sys
from importlib import import_module
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.context.pipeline import ContextPipeline  # noqa: E402
from app.core.context.view import ContextView  # noqa: E402
from app.core.infra import token_estimator  # noqa: E402
from app.core.infra.token_estimator import count_messages_tokens, per_message_tokens  # noqa: E402


def _big_tool(i: int, size: int = 40_000) -> dict:
    return {
        "role": "tool",
        "name": "read",
        "tool_call_id": f"c{i}",
        "content": f"result-{i} " + "x" * size,
    }


def _base_messages(n: int = 800, big_at=None):
    msgs = []
    big_at = set(big_at or ())
    for i in range(n):
        if i in big_at:
            msgs.append(_big_tool(i))
        else:
            msgs.append({"role": "user", "content": f"m-{i} hello"})
    return msgs


def _make_view(n=800, big_at=None):
    return ContextView(messages=_base_messages(n, big_at), stage="send")


def _prune_tier():
    return import_module("plugins.system-context.context_tiers.tool_prune").ToolPruneTier()


def _normalize_tier():
    return import_module("plugins.system-context.context_tiers.tool_normalize").ToolNormalizeTier()


def _shape_tier():
    return import_module("plugins.system-context.context_tiers.tool_shape").ToolResultShapeTier()


def test_prune_saved_matches_full_recount_n800():
    """N=800：pipeline 增量 saved == 全量重算（ratio 全 1.00 严格恒等）。"""
    view = _make_view(800, big_at={10, 100, 300, 500, 700})
    before = view.used_tokens
    outcome = _prune_tier().apply(view)

    # 全量口径（原实现的 after 计算）：整表 count_messages_tokens
    saved_full = before - count_messages_tokens(outcome.messages)
    # pipeline 增量口径：replace 后 used 差
    view.replace_messages(outcome.messages)
    saved_incr = max(0, before - view.used_tokens)

    assert outcome.applied
    assert saved_full > 0
    assert saved_incr == saved_full, (
        f"增量口径与全量重算必须严格一致：incr={saved_incr} full={saved_full}"
    )


@pytest.mark.parametrize("tier_mod,tier_cls,make_msg", [
    ("tool_normalize", "ToolNormalizeTier", None),
    ("tool_prune", "ToolPruneTier", None),
    ("tool_shape", "ToolResultShapeTier", None),
])
def test_three_tiers_no_full_recount(tier_mod, tier_cls, make_msg):
    """三层 apply 内不再全量遍历：count_messages_tokens 调用次数 == 0。"""
    mod = import_module(f"plugins.system-context.context_tiers.{tier_mod}")
    tier = getattr(mod, tier_cls)()

    calls = {"n": 0}
    original = token_estimator.count_messages_tokens

    def counting(*a, **k):
        calls["n"] += 1
        return original(*a, **k)

    token_estimator.count_messages_tokens = counting
    try:
        view = _make_view(800, big_at={5, 200, 600})
        outcome = tier.apply(view)
    finally:
        token_estimator.count_messages_tokens = original

    assert outcome.applied
    assert calls["n"] == 0, f"{tier_cls}.apply 内仍有全量 count_messages_tokens 调用 {calls['n']} 次"


def test_pipeline_saved_semantics_via_replace(monkeypatch):
    """端到端：pipeline.run 产出的 TierStat.saved_tokens == before-after 且 >0。"""
    from app.plugins.contracts.context_policy import ContextTier, TierOutcome

    class _BigToolTier:
        id = "big_tool_stub"
        label = "stub"
        order = 15
        stages = frozenset({"send"})
        cache_impact = "none"

        def should_apply(self, view):
            return any(len(m.get("content") or "") > 100 for m in view.messages)

        def apply(self, view):
            result = []
            for m in view.messages:
                c = m.get("content")
                if isinstance(c, str) and len(c) > 100:
                    m = {**m, "content": c[:50] + "...stub"}
                result.append(m)
            # tier 不再自报 saved（契约 [#R6]），pipeline 统一计
            return TierOutcome(messages=result, saved_tokens=0, note="stub 截断")

    view = _make_view(400, big_at=set(range(0, 400, 40)))
    pipeline = ContextPipeline.__new__(ContextPipeline)
    pipeline._chain = lambda stage: [_BigToolTier()]
    pipeline._allow = lambda tier, view_: True

    ran = pipeline.run(view)
    assert ran.stats, "应有 TierStat 记录"
    stat = ran.stats[0]
    assert stat.saved_tokens == stat.before_tokens - stat.after_tokens > 0


def test_circuit_breaker_uses_incremental_saved(monkeypatch):
    """熔断判定走增量 saved：连续无收益（before==after）→ note_failure 计数递增。"""
    from app.plugins.contracts.context_policy import TierOutcome

    class _NoopTier:
        id = "noop"
        label = "noop"
        order = 15
        stages = frozenset({"send"})
        cache_impact = "none"

        def should_apply(self, view):
            return True

        def apply(self, view):
            # applied 但内容未变 → saved==0 → 熔断计数
            return TierOutcome(messages=list(view.messages), saved_tokens=0, note="无变化")

    view = _make_view(50)
    pipeline = ContextPipeline.__new__(ContextPipeline)
    pipeline._chain = lambda stage: [_NoopTier()]
    pipeline._allow = lambda tier, view_: True
    pipeline.run(view)
    assert view.failure_counts.get("noop") == 1, "首轮无收益应计数 1"
    pipeline.run(view)
    assert view.failure_counts.get("noop") >= 2, "连续无收益应触发熔断计数（TIER_FAILURE_LIMIT=2）"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
