# -*- coding: utf-8 -*-
"""子智能体 hook 触发策略 — 系统插件实现（id="subagent_default"，scope="subagent"）。

子智能体的 hook 触发集合与主智能体差异：
- 工具级（PreToolUse/PostToolUse）：保留（子智能体的工具调用是核心）
- Stop：保留（子智能体需感知停止事件做最终总结）
- PluginChanged：保留（环境事件同步给子智能体）
- SessionStart/PreUserMessage/PostUserMessage：跳过（子智能体无独立会话生命周期）
- PreAssistantMessage/PostAssistantMessage：跳过（子智能体自注入由 worker 内部触发，
  不走全局 hook_policy 控制）
- UserPromptSubmit：跳过（子智能体无用户提问环节）
- BuildSystemPrompt：跳过（子智能体使用子 agent 配置，不走主系统 prompt 注入）
- TeamMail：跳过（子智能体不参与团队邮件）
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
    StopEvent,
)


class SubagentDefaultHookPolicy:
    """子智能体默认 hook 触发策略 — 子智能体域"""

    id = "subagent_default"
    scope = "subagent"

    def should_trigger(self, event: HookEvent) -> HookDecision:
        if isinstance(event, (PreToolUseEvent, PostToolUseEvent, StopEvent, PluginChangedEvent)):
            return HookDecision.TRIGGER
        return HookDecision.SKIP


def register(registry):
    registry.register(SubagentDefaultHookPolicy())
