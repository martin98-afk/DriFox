# -*- coding: utf-8 -*-
"""order 30 — 重复工具结果去重：相同内容只保留最新一份完整拷贝。

来源：_prune_old_tool_results Pass 1（history_compactor.py）。
cache_impact=invalidate：改写旧 tool 消息 content，破坏前缀。
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List

from app.core.context.tiers import count_tokens
from app.plugins.contracts.context_policy import CACHE_INVALIDATE, STAGE_SEND, STAGE_UI, TierOutcome

DEDUP_MIN_CHARS = 200  # 短于此不参与去重（与现状一致）
_DUP_NOTE = "[Duplicate tool output — same content as a more recent call]"


def _hash(content: str) -> str:
    return hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()[:12]


class ToolDedupeTier:
    id = "tool_dedupe"
    label = "重复工具结果去重"
    order = 30
    stages = frozenset({STAGE_SEND, STAGE_UI})
    cache_impact = CACHE_INVALIDATE

    def should_apply(self, view) -> bool:
        seen = set()
        for m in view.messages:
            if m.get("role") != "tool":
                continue
            c = m.get("content")
            if not isinstance(c, str) or len(c) < DEDUP_MIN_CHARS:
                continue
            h = _hash(c)
            if h in seen:
                return True
            seen.add(h)
        return False

    def apply(self, view) -> TierOutcome:
        result: List[Dict[str, Any]] = list(view.messages)
        seen = set()
        pruned = 0
        # 从后向前：保留最新一份完整拷贝，旧副本替换为反向引用
        for i in range(len(result) - 1, -1, -1):
            msg = result[i]
            if msg.get("role") != "tool":
                continue
            c = msg.get("content")
            if not isinstance(c, str) or len(c) < DEDUP_MIN_CHARS:
                continue
            h = _hash(c)
            if h in seen:
                result[i] = {**msg, "content": _DUP_NOTE}
                pruned += 1
            else:
                seen.add(h)
        if pruned == 0:
            return TierOutcome(messages=list(view.messages), saved_tokens=0, note="无重复")
        saved = view.used_tokens - count_tokens(result)
        return TierOutcome(
            messages=result, saved_tokens=max(0, saved), note=f"去重 {pruned} 条重复工具结果"
        )
