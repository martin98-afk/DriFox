# -*- coding: utf-8 -*-
"""循环策略契约 — Agent Loop 的继续/终止判定收敛为可插拔策略。

现有 worker 行为 → DefaultLoopPolicy 语义对照：
- tool_calls_found → 续循环执行工具（chat_worker.run while 自然继续）
- stop_hook_injected → 续命一轮（_stop_hook_active 单次放行机制）
- repetitive_loop_detected → 静默清理后继续（_detect_repetitive_tool_loop 路径）
- 全否 → 自然完成退出
max_rounds 返回 None 表示不限（与现状一致），插件可返回小值实现极简模式。
max_rounds 计的是 while 迭代次数（含流式 pending/续命 continue），N 实际允许 N 次 API 调用。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Protocol, runtime_checkable


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

    def should_continue(self, state: LoopState) -> LoopDecision:
        ...

    def max_rounds(self, llm_config: Dict[str, Any]) -> Optional[int]:
        """最大循环轮数（None=不限）"""
        ...
