# -*- coding: utf-8 -*-
"""上下文预算与用量（只读）。

历史压缩器退役后留下的只读能力：两个函数都不修改消息、不触发任何有损操作。
调用方：消息构建（builder）、worker 轮询（context_updated 圆环分母）、
gateway `/status`、UI 用量快照。

预算口径与退役前的 HistoryCompactor.get_budget 完全一致，不要随手改：
    预算 = 上下文窗口 - 输出预留，下限 500
    输出预留：常规 min(800, max_output_tokens)；o1/o3 模型取 max_output_tokens（上限 32000）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.core.infra.token_estimator import count_messages_tokens
from app.core.modelmeta.model_capabilities import resolve_context_limit, resolve_max_output_tokens

MIN_CONTEXT_BUDGET = 500
DEFAULT_OUTPUT_RESERVE = 800
REASONING_OUTPUT_RESERVE = 32000

# 推理模型（o1/o3 系列）需要更大的输出预留
_REASONING_MODEL_MARKS = ("o1", "o3")


def resolve_context_budget(llm_config: Optional[Dict[str, Any]] = None) -> int:
    """可用上下文预算（token）。

    Args:
        llm_config: 模型配置；不传按无配置处理（走模型能力兜底）
    """
    cfg = llm_config or {}
    context_limit = resolve_context_limit(cfg)
    max_output_tokens = resolve_max_output_tokens(cfg)

    model_name = str(cfg.get("model", "")).lower()
    reserved = min(DEFAULT_OUTPUT_RESERVE, max_output_tokens)
    if any(mark in model_name for mark in _REASONING_MODEL_MARKS):
        reserved = min(max_output_tokens, REASONING_OUTPUT_RESERVE)

    return max(MIN_CONTEXT_BUDGET, context_limit - reserved)


def compute_usage(messages: List[Dict[str, Any]], budget: int) -> Dict[str, Any]:
    """用量统计：{used_tokens, budget_tokens, percent}（不修改 messages）。"""
    used_tokens = count_messages_tokens(messages)
    budget_tokens = max(1, budget)
    percent = max(0, min(100, int((used_tokens / budget_tokens) * 100)))
    return {
        "used_tokens": used_tokens,
        "budget_tokens": budget_tokens,
        "percent": percent,
    }


def empty_compaction_state() -> Dict[str, Any]:
    """空压缩状态。

    `compaction_state` 仍是会话持久化 schema 的一部分（UI 读 `state["active"]`
    决定是否显示「已压缩」标记）。压缩器退役后恒为空态，字段结构保持不变。
    """
    return {
        "active": False,
        "source": "history",
        "kind": "structured",
        "original_count": 0,
        "summarized_count": 0,
        "kept_count": 0,
        "summary_count": 0,
        "note": "",
    }
