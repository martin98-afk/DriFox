# -*- coding: utf-8 -*-
"""order 80 — LLM 摘要（最后手段）：前一层未达标时才触发，允许调 LLM。

来源：HistoryCompactor._do_compact 的 _summarize(allow_llm=True) 路径
（_summarize_with_llm / _summarize_heuristic）。
stage 仅 send：ui 估算绝不允许触发网络调用。
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.core.context.tiers import count_tokens
from app.plugins.contracts.context_policy import CACHE_INVALIDATE, STAGE_SEND, TierOutcome


class LlmSummaryTier:
    id = "llm_summary"
    label = "LLM 摘要压缩"
    order = 80
    stages = frozenset({STAGE_SEND})
    cache_impact = CACHE_INVALIDATE

    def should_apply(self, view) -> bool:
        # cascade 已保证只有未达标才走到这层；此判据为二次保险
        return view.used_tokens > view.target_tokens

    def apply(self, view) -> TierOutcome:
        from app.core.history_compactor import HistoryCompactor

        compactor = HistoryCompactor(lambda: view.llm_config or {}, None)
        messages, state, cache = compactor.compact(
            view.messages,
            view.budget,
            existing_cache=view.compaction_cache or None,
            allow_llm_summary=True,
            prenormalized=view.messages,
        )
        view.compaction_state = state
        view.compaction_cache = cache
        if not messages:
            return TierOutcome(messages=list(view.messages), saved_tokens=0, note="LLM 摘要产出为空")
        saved = view.used_tokens - count_tokens(messages)
        return TierOutcome(messages=messages, saved_tokens=max(0, saved), note="LLM 摘要压缩")
