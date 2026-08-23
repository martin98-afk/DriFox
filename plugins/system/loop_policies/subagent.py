# -*- coding: utf-8 -*-
"""子智能体循环策略 — 系统插件实现（id="subagent"，scope="subagent"）。

从 subagent_worker 硬编码逻辑拆出（行为等价迁移）：
- 轮数限制：max_rounds 默认 30（配置键「子智能体最大轮数」可调；
  per-agent steps 声明在 worker 层优先，本策略为无声明时的兜底）
- 最后总结机制：达到轮数上限时注入 final_summary_prompt()，
  驱动一次无工具 API 调用产出结构化收尾
- should_continue：有工具调用则继续；无工具自然完成（子智能体无 Stop hook /
  重复循环检测路径，对应标志恒 False，防御性镜像 default 语义）
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.plugins.contracts.loop_policy import LoopDecision, LoopPolicy, LoopState

DEFAULT_SUBAGENT_MAX_ROUNDS = 30


class SubagentLoopPolicy:
    """子智能体策略：轮数限制 + 最后总结机制"""

    id = "subagent"
    scope = "subagent"

    def should_continue(self, state: LoopState) -> LoopDecision:
        if state.repetitive_loop_detected:  # 防御：与 default 对齐，当前 worker 不触发
            return LoopDecision.CONTINUE
        if state.tool_calls_found:
            return LoopDecision.CONTINUE
        return LoopDecision.STOP

    def max_rounds(self, llm_config: Dict[str, Any]) -> Optional[int]:
        """默认 30 轮（与原硬编码 max_iterations=30 等价）；配置键可调"""
        try:
            v = llm_config.get("子智能体最大轮数") if llm_config else None
            return int(v) if v else DEFAULT_SUBAGENT_MAX_ROUNDS
        except TypeError, ValueError:
            return DEFAULT_SUBAGENT_MAX_ROUNDS

    def final_summary_prompt(self) -> str:
        """达到轮数上限时的最终总结提示（与原 _build_final_summary_prompt 逐字等价）"""
        return """

## 已达到最大迭代次数限制，请总结当前执行结果。

请整理并返回以下内容：
1. 已完成的工作
2. 关键发现和结论
3. 重要文件路径和数据
4. 待解决的问题（如有）

直接输出总结内容："""


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。"""
    registry.register(SubagentLoopPolicy())
