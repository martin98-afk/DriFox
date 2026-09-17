# -*- coding: utf-8 -*-
"""order 10 — 历史图片剥离：只保留最后一个带图 user 消息及其之后的图片块。

来源：HistoryCompactor._prune_large_tool_outputs 的第一步（_strip_historical_media）。
cache_impact=none：本层在 cascade 最前、早于任何前缀命中窗口，
与 Claude Code 的 Budget Reduction 同层语义。
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.core.context.tiers import count_tokens
from app.plugins.contracts.context_policy import CACHE_NONE, STAGE_SEND, STAGE_UI, TierOutcome

_IMAGE_TYPES = ("image_url", "input_image", "image")


def _has_image_content(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    return any(isinstance(p, dict) and p.get("type") in _IMAGE_TYPES for p in content)


class MediaStripTier:
    id = "media_strip"
    label = "历史图片剥离"
    order = 10
    stages = frozenset({STAGE_SEND, STAGE_UI})
    cache_impact = CACHE_NONE

    def should_apply(self, view) -> bool:
        return any(
            m.get("role") == "user" and _has_image_content(m.get("content")) for m in view.messages
        )

    def apply(self, view) -> TierOutcome:
        from app.core.history_compactor import _strip_historical_media

        new_messages: List[Dict[str, Any]] = _strip_historical_media(view.messages)
        if new_messages is view.messages:
            return TierOutcome(messages=list(view.messages), saved_tokens=0, note="无历史图片")
        saved = view.used_tokens - count_tokens(new_messages)
        return TierOutcome(messages=new_messages, saved_tokens=max(0, saved), note="剥离历史图片块")
