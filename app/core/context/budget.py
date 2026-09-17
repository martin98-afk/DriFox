# -*- coding: utf-8 -*-
"""内置预算解析器 — 合并 HistoryCompactor.get_budget 与
ContextBudgetAllocator._allocate_history_budget 的计算，产出 (budget, target_tokens)。

budget         = 历史消息可用 token（上下文上限 − 输出预留 − 系统提示挤压）
target_tokens  = budget * target_ratio，cascade 达标线（used <= 此值即停）
"""

from __future__ import annotations

from collections import OrderedDict
from hashlib import md5
from typing import Any, Dict

from app.core.context.config import cfg_ratio
from app.core.model_capabilities import resolve_context_limit, resolve_max_output_tokens
from app.core.token_estimator import count_messages_tokens
from app.plugins.contracts.context_policy import BudgetResult

SYSTEM_PROMPT_RESERVE_RATIO = 0.15
MIN_HISTORY_BUDGET_RATIO = 0.60


class BuiltInBudgetResolver:
    """内置预算解析器（id="builtin"）"""

    id = "builtin"

    def __init__(self) -> None:
        self._system_tokens_cache: "OrderedDict[str, int]" = OrderedDict()

    def resolve(self, llm_config: Dict[str, Any], system_content: str = "") -> BudgetResult:
        llm_config = llm_config or {}
        try:
            context_limit = resolve_context_limit(llm_config)
            max_output = resolve_max_output_tokens(llm_config)
        except Exception:
            context_limit, max_output = 128000, 4096

        model_name = str(llm_config.get("model", "")).lower()
        # O1/O3 需要更大的输出预留
        reserved = min(800, max_output)
        if "o1" in model_name or "o3" in model_name:
            reserved = min(max_output, 32000)
        total_budget = max(500, context_limit - reserved)

        # 系统提示占比挤压历史预算（同 _allocate_history_budget）
        if system_content:
            system_tokens = self._cached_system_tokens(system_content)
            system_ratio = system_tokens / max(total_budget, 1)
            history_budget = (
                int(total_budget * (1 - system_ratio))
                if system_ratio > SYSTEM_PROMPT_RESERVE_RATIO
                else int(total_budget * MIN_HISTORY_BUDGET_RATIO)
            )
        else:
            history_budget = int(total_budget * MIN_HISTORY_BUDGET_RATIO)
        history_budget = max(500, history_budget)

        target_ratio = cfg_ratio("target_ratio", 0.6)
        return BudgetResult(budget=history_budget, target_tokens=max(1, int(history_budget * target_ratio)))

    def _cached_system_tokens(self, content: str) -> int:
        key = md5(content.encode("utf-8")).hexdigest()
        cached = self._system_tokens_cache.get(key)
        if cached is not None:
            self._system_tokens_cache.move_to_end(key)
            return cached
        try:
            tokens = count_messages_tokens([{"role": "system", "content": content}])
        except Exception:
            tokens = 0
        self._system_tokens_cache[key] = tokens
        if len(self._system_tokens_cache) > 64:
            self._system_tokens_cache.popitem(last=False)
        return self._system_tokens_cache[key]
