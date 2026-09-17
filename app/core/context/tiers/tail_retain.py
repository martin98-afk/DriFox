# -*- coding: utf-8 -*-
"""order 70 — 尾保留 + 单条超大消息截断。

来源：HistoryCompactor._do_compact 的尾保留段（从后向前累积，不拆分 tool 配对，
含 hard_tail_cap 硬上限与 MAX_SINGLE_MESSAGE_RATIO 单条截断）。

不重写算法：构造轻量 HistoryCompactor 并复用其 compact 的尾保留逻辑，
tier 只负责 trigger 判定与结果包装。摘要部分在 order 80 单独一层。
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.core.context.config import cfg_ratio
from app.core.context.tiers import count_tokens
from app.plugins.contracts.context_policy import CACHE_INVALIDATE, STAGE_SEND, STAGE_UI, TierOutcome

RECENT_HISTORY_MIN_MESSAGES = 6
MAX_SINGLE_MESSAGE_RATIO = 0.15
MAX_TAIL_OVERFLOW_MULTIPLIER = 2.5
MAX_TOOL_CONTENT_CHARS = 3000
MAX_HISTORY_SNIPPET_CHARS = 1200


class TailRetainTier:
    id = "tail_retain"
    label = "尾保留截断"
    order = 70
    stages = frozenset({STAGE_SEND, STAGE_UI})
    cache_impact = CACHE_INVALIDATE

    def should_apply(self, view) -> bool:
        soft_ratio = cfg_ratio("soft_limit_ratio", 0.84)
        return view.used_tokens > max(1, int(view.budget * soft_ratio))

    def apply(self, view) -> TierOutcome:
        from app.core.history_compactor import HistoryCompactor

        compactor = HistoryCompactor(lambda: view.llm_config or {}, None)
        # allow_llm_summary=False：本层只做尾保留 + 单条截断，
        # LLM 摘要留给 order 80（cascade 达标即停，未达标才升级）
        messages, state, cache = compactor.compact(
            view.messages,
            view.budget,
            existing_cache=view.compaction_cache or None,
            allow_llm_summary=False,
            prenormalized=view.messages,
        )
        view.compaction_state = state
        view.compaction_cache = cache
        if not messages:
            return TierOutcome(messages=list(view.messages), saved_tokens=0, note="压缩产出为空")
        saved = view.used_tokens - count_tokens(messages)
        return TierOutcome(
            messages=messages,
            saved_tokens=max(0, saved),
            note=f"尾保留至 {len(messages)}/{len(view.messages)} 条",
        )
