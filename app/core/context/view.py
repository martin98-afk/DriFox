# -*- coding: utf-8 -*-
"""上下文投影对象 — cascade 的输入/输出载体。

ContextView 是「给模型看的那一份上下文」的临时视图。tier 只读取它、
只产出新 messages 列表，不原地修改 —— 保证 tier 幂等、可热替换、可观测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.core.token_estimator import count_messages_tokens


@dataclass
class TierStat:
    """单层执行记录（供 UI / 日志展示每层省了多少、是否破坏 cache 前缀）"""

    tier_id: str
    label: str
    before_tokens: int = 0
    after_tokens: int = 0
    saved_tokens: int = 0
    cache_impact: str = ""
    note: str = ""
    skipped: bool = False  # True = 抛错被跳过


@dataclass
class ContextView:
    """一次上下文投影的完整状态。"""

    messages: List[Dict[str, Any]]
    budget: int = 0
    target_tokens: int = 0
    llm_config: Dict[str, Any] = field(default_factory=dict)
    stage: str = "send"
    stats: List[TierStat] = field(default_factory=list)
    compaction_state: Dict[str, Any] = field(default_factory=dict)
    compaction_cache: Dict[str, Any] = field(default_factory=dict)
    failure_counts: Dict[str, int] = field(default_factory=dict)

    # used_tokens 惰性估算：messages 被替换后失效重算
    _used_cache: Optional[int] = field(default=None, repr=False, compare=False)

    @property
    def used_tokens(self) -> int:
        if self._used_cache is None:
            try:
                self._used_cache = count_messages_tokens(self.messages)
            except Exception:
                self._used_cache = 0
        return self._used_cache

    def replace_messages(self, new_messages: List[Dict[str, Any]]) -> None:
        """唯一允许的 messages 写入口：同步失效 token 缓存。"""
        self.messages = new_messages
        self._used_cache = None

    def record(self, stat: TierStat) -> None:
        self.stats.append(stat)

    def note_failure(self, tier_id: str) -> int:
        """连续无收益计数 +1，返回当前值"""
        n = self.failure_counts.get(tier_id, 0) + 1
        self.failure_counts[tier_id] = n
        return n

    def clear_failure(self, tier_id: str) -> None:
        self.failure_counts.pop(tier_id, None)
