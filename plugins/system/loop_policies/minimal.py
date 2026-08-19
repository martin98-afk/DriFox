# -*- coding: utf-8 -*-
"""极简循环策略 — 对应 DSH「极简模式」：单轮完成，无工具迭代、无续命。

激活方式（Phase A，无 UI）：
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry
    LoopPolicyRegistry.get_instance().set_active("minimal")
恢复默认：
    LoopPolicyRegistry.get_instance().set_active("default")
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.plugins.contracts.loop_policy import LoopDecision, LoopPolicy, LoopState


class MinimalLoopPolicy:
    """极简策略：无论工具调用/注入，一律单轮即停"""

    id = "minimal"

    def should_continue(self, state: LoopState) -> LoopDecision:
        return LoopDecision.STOP

    def max_rounds(self, llm_config: Dict[str, Any]) -> Optional[int]:
        return 1


def register(registry):
    registry.register(MinimalLoopPolicy())
