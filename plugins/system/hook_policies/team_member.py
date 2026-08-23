# -*- coding: utf-8 -*-
"""团队成员窗口 hook 触发策略 — 系统插件实现（id="team_member"，scope="team_member"）。

团队成员窗口的 hook 触发集合与主智能体差异（成员 = 邮件驱动的对话流）：
- 工具级（PreToolUse/PostToolUse）：保留（成员需执行工具完成任务）
- SessionStart：保留（成员入会时启动提示）
- Stop：保留（成员结束对话时通知）
- TeamMail：保留（成员感知团队邮件到达）
- PluginChanged：保留（环境事件同步）
- PreAssistantMessage/PostAssistantMessage：**跳过**（成员窗口的助手回复由邮件驱动
  注入，不应被主对话语义 hook 拦截；避免污染成员的会话流边界）
- PreUserMessage/PostUserMessage：跳过（成员无独立用户交互）
- UserPromptSubmit：跳过（成员无用户提问）
- BuildSystemPrompt：跳过（成员继承团队配置，不走主系统 prompt 注入）
"""

from __future__ import annotations

from typing import Any

from app.plugins.contracts.hook_policy import (
    HookDecision,
    HookEvent,
    HookPolicy,
    PluginChangedEvent,
    PostToolUseEvent,
    PreToolUseEvent,
    SessionStartEvent,
    StopEvent,
    TeamMailEvent,
)


class TeamMemberHookPolicy:
    """团队成员默认 hook 触发策略 — 团队成员域"""

    id = "team_member"
    scope = "team_member"

    def should_trigger(self, event: HookEvent) -> HookDecision:
        if isinstance(
            event,
            (
                PreToolUseEvent,
                PostToolUseEvent,
                SessionStartEvent,
                StopEvent,
                TeamMailEvent,
                PluginChangedEvent,
            ),
        ):
            return HookDecision.TRIGGER
        return HookDecision.SKIP


def register(registry):
    registry.register(TeamMemberHookPolicy())
