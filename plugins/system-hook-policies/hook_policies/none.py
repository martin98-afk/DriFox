# -*- coding: utf-8 -*-
"""全局 hook 全跳过策略 — 系统插件实现（id="none"，scope="main"）。

行为兼容原 HookPolicy.NONE：跳过所有 hook 事件。
- 插件后台循环默认（如象棋每步棋）
- 完全不触发任何全局 hook
"""

from __future__ import annotations


from app.plugins.contracts.hook_policy import HookDecision, HookEvent


class NoneHookPolicy:
    """全跳过 hook 策略 — 主智能体域"""

    id = "none"
    scope = "main"

    def should_trigger(self, event: HookEvent) -> HookDecision:
        return HookDecision.SKIP


def register(registry):
    registry.register(NoneHookPolicy())
