# -*- coding: utf-8 -*-
"""仅工具级 hook 触发策略 — 系统插件实现（id="tool_only"，scope="main"）。

行为兼容原 HookPolicy.TOOL_EVENTS_ONLY：仅触发 PreToolUse / PostToolUse。
- 插件自建引擎的安全审查类 hook 仍生效
- 主对话语义的全局 hook（PreAssistant/PostAssistant/Stop 等）跳过
"""

from __future__ import annotations

from typing import Any

from app.plugins.contracts.hook_policy import (
    HookDecision,
    HookEvent,
    HookPolicy,
    PostToolUseEvent,
    PreToolUseEvent,
)


class ToolOnlyHookPolicy:
    """仅工具级 hook 触发策略 — 主智能体域"""

    id = "tool_only"
    scope = "main"

    def should_trigger(self, event: HookEvent) -> HookDecision:
        if isinstance(event, (PreToolUseEvent, PostToolUseEvent)):
            return HookDecision.TRIGGER
        return HookDecision.SKIP


def register(registry):
    registry.register(ToolOnlyHookPolicy())
