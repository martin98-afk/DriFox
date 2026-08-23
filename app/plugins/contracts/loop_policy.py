# -*- coding: utf-8 -*-
"""循环策略契约 — Agent Loop 的继续/终止判定收敛为可插拔策略。

现有 worker 行为 → DefaultLoopPolicy 语义对照：
- tool_calls_found → 续循环执行工具（chat_worker.run while 自然继续）
- stop_hook_injected → 续命一轮（_stop_hook_active 单次放行机制）
- repetitive_loop_detected → 静默清理后继续（_detect_repetitive_tool_loop 路径）
- 全否 → 自然完成退出
max_rounds 返回 None 表示不限（与现状一致），插件可返回小值实现极简模式。
max_rounds 计的是 while 迭代次数（含流式 pending/续命 continue），N 实际允许 N 次 API 调用。

scope 分域（v2）：策略声明自身归属，注册表按 scope 维护独立激活槽
- "main"     主智能体（chat_worker）：不限轮数 + Stop hook 续命（DefaultLoopPolicy）
- "subagent" 子智能体（subagent_worker）：轮数限制 + 最后总结机制（SubagentLoopPolicy）
旧策略未声明 scope 时注册表按 "main" 兜底（向后兼容）。

final_summary_prompt（可选）：子智能体策略实现，达到轮数上限时注入的
最终总结提示词，驱动一次无工具 API 调用产出结构化收尾。主智能体策略可不实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Protocol, runtime_checkable

SCOPE_MAIN = "main"
SCOPE_SUBAGENT = "subagent"


class LoopDecision(Enum):
    CONTINUE = "continue"
    STOP = "stop"


@dataclass
class LoopState:
    """一轮循环的判定输入（worker 每轮构造一次）"""

    round_count: int = 0
    tool_calls_found: bool = False
    stop_hook_injected: bool = False
    repetitive_loop_detected: bool = False


@runtime_checkable
class LoopPolicy(Protocol):
    """循环策略接口"""

    id: str
    scope: str  # SCOPE_MAIN / SCOPE_SUBAGENT；未声明的旧策略按 main 兜底

    def should_continue(self, state: LoopState) -> LoopDecision: ...

    def max_rounds(self, llm_config: Dict[str, Any]) -> Optional[int]:
        """最大循环轮数（None=不限）"""
        ...

    def final_summary_prompt(self) -> str:
        """达到轮数上限时的最终总结提示词（子智能体策略实现；主智能体策略可不实现）"""
        ...
