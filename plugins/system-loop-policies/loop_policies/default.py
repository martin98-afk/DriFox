# -*- coding: utf-8 -*-
"""默认循环策略 — 系统插件实现（id="default"）。

行为零变化原则：与现有 chat_worker.run() 行为逐点等价。
- repetitive_loop_detected → CONTINUE（静默清理后继续）
- tool_calls_found        → CONTINUE
- stop_hook_injected      → CONTINUE（Stop hook 续命一轮）
- 其它                   → STOP
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.plugins.contracts.loop_policy import LoopDecision, LoopState


class DefaultLoopPolicy:
    """默认循环策略 — 与现有 chat_worker.run() 行为逐点等价（主智能体域）"""

    id = "default"
    scope = "main"

    def should_continue(self, state: LoopState) -> LoopDecision:
        if state.repetitive_loop_detected:
            return LoopDecision.CONTINUE  # 现状：静默清理后继续
        if state.tool_calls_found:
            return LoopDecision.CONTINUE
        if state.stop_hook_injected:
            return LoopDecision.CONTINUE  # 现状：Stop hook 续命一轮
        return LoopDecision.STOP

    def max_rounds(self, llm_config: Dict[str, Any]) -> Optional[int]:
        """默认不限轮数（现状 while 无上限）；配置键可设上限"""
        try:
            v = llm_config.get("最大循环轮数") if llm_config else None
            return int(v) if v else None
        except TypeError, ValueError:
            return None


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。

    source 由 loader 的 _RegistryProxy 强制为 "plugin:system"，
    本函数不显式传入，保持与 tools/providers 插件约定一致。
    """
    registry.register(DefaultLoopPolicy())
