# -*- coding: utf-8 -*-
"""上下文策略契约 — 上下文管理分层降级管线（tier cascade）的可插拔接口。

范式来源：Claude Code src/services/compact 的多层降级 + Anthropic context editing
的声明式 trigger 参数。核心思想：
  1. 上下文管理不是一堆孤立截断函数，而是一条「由轻到重」的 cascade，
     每跑一层重新度量，达标即停；
  2. 每一层自带 trigger 声明（何时够格跑）与 order（跑在第几位）；
  3. 层对输入只读，只产出新消息列表 —— 保证幂等与可替换。

stage 语义（同一 cascade 三个入口共用）：
  "ingest" 工具结果入库前 —— 允许副作用（落盘写文件）
  "send"   组装发给 LLM 的上下文 —— 禁止副作用
  "ui"     UI 用量估算 —— 禁止副作用（只读）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Protocol, runtime_checkable

STAGE_INGEST = "ingest"
STAGE_SEND = "send"
STAGE_UI = "ui"
ALL_STAGES: FrozenSet[str] = frozenset({STAGE_INGEST, STAGE_SEND, STAGE_UI})

# cache_impact 取值
CACHE_NONE = "none"  # 不动 prompt cache 前缀
CACHE_PRESERVE = "preserve"  # 保持前缀稳定
CACHE_INVALIDATE = "invalidate"  # 破坏前缀，cache 失效

# 连续无收益上限：达到即熔断该 tier（对齐 HistoryCompactor 反抖动语义）
TIER_FAILURE_LIMIT = 2


@dataclass(frozen=True)
class TierOutcome:
    """tier.apply 的返回。

    messages 必须是新列表（tier 不得原地修改 view.messages）。
    """

    messages: List[Dict[str, Any]]
    saved_tokens: int = 0
    note: str = ""
    applied: bool = True

    @staticmethod
    def skip() -> "TierOutcome":
        """trigger 已过但本层实际未改动（用于幂等判定：saved_tokens=0）"""
        return TierOutcome(messages=[], saved_tokens=0, note="", applied=False)


@dataclass(frozen=True)
class BudgetResult:
    """预算解析器产出：budget 为可用预算，target_tokens 为 cascade 达标线。"""

    budget: int
    target_tokens: int


@runtime_checkable
class ContextTier(Protocol):
    """上下文降级层。

    四条硬约束（违反会导致 cascade 行为不可预测）：
      1. 只读输入：不得修改 view.llm_config / budget / target_tokens / stage
      2. 异常隔离：抛错由 pipeline 捕获并跳过（本层不吞异常，交给 pipeline）
      3. 幂等：同一 view 重复 apply 应产出 saved_tokens == 0
      4. 阈值外置：不得硬编码阈值，一律走 config（app.core.context.config）
    """

    id: str  # 覆盖键；插件注册同 id 覆盖内置
    label: str
    order: int  # 10 起步步长 10；同 order 后注册者覆盖先注册者
    stages: FrozenSet[str]  # 该 tier 在哪些 stage 生效
    cache_impact: str  # CACHE_NONE / CACHE_PRESERVE / CACHE_INVALIDATE

    def should_apply(self, view: Any) -> bool:
        """trigger：当前 view 是否需要本层介入"""
        ...

    def apply(self, view: Any) -> TierOutcome:
        """执行降级，返回新消息列表与节省 token 数"""
        ...


@runtime_checkable
class ContextBudgetResolver(Protocol):
    """预算解析器 — cascade 的输入计算者（不是 tier：预算是输入而非步骤）。"""

    id: str

    def resolve(self, llm_config: Dict[str, Any], system_content: str = "") -> BudgetResult: ...
