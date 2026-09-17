# -*- coding: utf-8 -*-
"""order 30 — 工具结果截断（S1）：超阈值 tool 输出分层截断，头 + 尾 + 省略标记。

来源：context_builder.build_messages 的 S1 循环。
stage=all：三个入口都要（ingest 先截断，落盘判定才准确）。
cache_impact=none：入口即截断，前后缀自始一致。
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.core.context.config import cfg_int
from app.core.context.tiers import count_tokens
from app.core.context.tool_prune import PRUNE_SKIP_TOOLS, prune_tool_result, resolve_tool_result_max_len
from app.plugins.contracts.context_policy import ALL_STAGES, CACHE_NONE, TierOutcome


def _count(messages: List[Dict[str, Any]]) -> int:
    return count_tokens(messages)


class ToolPruneTier:
    id = "tool_prune"
    label = "工具结果截断"
    order = 30
    stages = frozenset(ALL_STAGES)
    cache_impact = CACHE_NONE

    def should_apply(self, view) -> bool:
        limit = self._limit(view)
        return any(
            m.get("role") == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > limit
            for m in view.messages
        )

    def apply(self, view) -> TierOutcome:
        limit = self._limit(view)
        result: List[Dict[str, Any]] = []
        changed = 0
        for m in view.messages:
            if m.get("role") == "tool" and isinstance(m.get("content"), str):
                name = m.get("name", "")
                if self._skip(name):
                    result.append(m)
                    continue
                new_content = prune_tool_result(m["content"], max_len=limit, tool_name=name)
                if new_content != m["content"]:
                    m = {**m, "content": new_content}
                    changed += 1
            result.append(m)
        if changed == 0:
            return TierOutcome(messages=list(view.messages), saved_tokens=0, note="无超阈值结果")
        saved = view.used_tokens - _count(result)
        return TierOutcome(messages=result, saved_tokens=max(0, saved), note=f"截断 {changed} 条工具结果")

    def _limit(self, view) -> int:
        override = cfg_int("tool_result_max_len", 0)
        return override if override > 0 else resolve_tool_result_max_len(view.llm_config)

    @staticmethod
    def _skip(name: str) -> bool:
        """免裁剪判定：先查工具注册声明，再回退模块兜底名单。"""
        if not name:
            return False
        if name in PRUNE_SKIP_TOOLS:
            return True
        try:
            from app.tools.registry import ToolRegistry

            return ToolRegistry.get_instance().is_no_prune(name)
        except Exception:
            return False
