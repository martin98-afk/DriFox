# -*- coding: utf-8 -*-
"""全局 hook 全触发策略 — 系统插件实现（id="all"，scope="main"）。

行为兼容原 HookPolicy.ALL：触发所有 hook 事件。
- 主对话 UI 默认使用（行为零变化）
- 插件后台循环不应使用本策略（应改 tool_only / none）
"""

from __future__ import annotations


from app.plugins.contracts.hook_policy import HookDecision, HookEvent


class AllHookPolicy:
    """全触发 hook 策略 — 主智能体域，触发所有 hook 事件"""

    id = "all"
    scope = "main"

    def should_trigger(self, event: HookEvent) -> HookDecision:
        return HookDecision.TRIGGER


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。

    source 由 loader 的 _RegistryProxy 强制为 "plugin:system"。
    """
    registry.register(AllHookPolicy())
