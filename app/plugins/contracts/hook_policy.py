# -*- coding: utf-8 -*-
"""Hook 触发策略契约 — 每事件独立 Event 类 + 按 scope 分域的可插拔策略。

每个 hook 事件对应一个独立 dataclass 类（继承 HookEvent 基类），
插件可 isinstance 判定具体事件类型并访问专属字段（PluginChanged.action/
diff、PreToolUse.tool_name、Stop.reason 等）。

背景：
原 `app/core/conversation/config.py` 的 `HookPolicy` 是硬编码 Enum
（ALL / TOOL_EVENTS_ONLY / NONE），仅控制 main scope 的消息级 hook 触发。
本契约为统一扩展点：

- 按 scope 分域：主智能体（main）、子智能体（subagent）、团队成员（team_member）
- 每域独立激活槽，互不干扰
- 插件可自定义策略决定哪些事件触发（事件级粗筛 + 字段级细判）
- 向后兼容：ConversationConfig.hook_policy（HookPolicy 枚举）仍可使用，
  启动时按枚举值 → 默认插件 id 回落（ALL→"all" / TOOL_EVENTS_ONLY→"tool_only" /
  NONE→"none"），保持现有引擎声明零侵入。

scope 设计：
- "main"         主智能体（chat_worker）：UI / Gateway / 主对话引擎
- "subagent"     子智能体（subagent_worker）：批量分发子任务的执行循环
- "team_member"  智能体团队成员窗口（OpenAIChatToolWindow 团队成员 tab）：
                  团队成员的 hook 触发应与主对话隔离，避免成员窗口的
                  PreAssistant/PostAssistant 等主对话语义 hook 干扰成员的
                  邮件驱动对话流边界。
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Protocol, runtime_checkable

SCOPE_MAIN = "main"
SCOPE_SUBAGENT = "subagent"
SCOPE_TEAM_MEMBER = "team_member"


class HookDecision(Enum):
    """单次 hook 触发决策"""

    TRIGGER = "trigger"  # 触发该事件对应的 hook
    SKIP = "skip"  # 跳过（hook 不执行）


# ============================================================
# 每事件一个独立类（继承 HookEvent 基类）
# ============================================================


class HookEvent(ABC):
    """所有事件类的抽象基类

    插件在 should_trigger 中用 isinstance 判定具体事件类型。
    每个子类承载该事件的专属字段（如 PreToolUse.tool_name / PluginChanged.diff）。
    """


@dataclass
class BuildSystemPromptEvent(HookEvent):
    """系统 prompt 构建前触发"""

    current_role: str = "primary"  # primary / subagent
    agent_name: str = ""


@dataclass
class SessionStartEvent(HookEvent):
    """会话启动触发"""

    state: str = "startup"  # startup / resume / clear / compact
    is_team_member: bool = False
    team_run_id: str = ""
    agent_name: str = ""


@dataclass
class UserPromptSubmitEvent(HookEvent):
    """用户提问提交触发"""

    message: str = ""
    is_team_member: bool = False


@dataclass
class PreUserMessageEvent(HookEvent):
    """用户消息处理前触发"""

    message: str = ""
    is_team_member: bool = False


@dataclass
class PostUserMessageEvent(HookEvent):
    """用户消息处理后触发"""

    message: str = ""
    is_team_member: bool = False


@dataclass
class PreAssistantMessageEvent(HookEvent):
    """助手回复前触发"""

    message: str = ""  # 基于用户消息的上下文
    is_team_member: bool = False


@dataclass
class PostAssistantMessageEvent(HookEvent):
    """助手回复后触发"""

    message: str = ""
    is_team_member: bool = False


@dataclass
class PreToolUseEvent(HookEvent):
    """工具调用前触发"""

    tool_name: str = ""
    tool_args: Dict[str, Any] = field(default_factory=dict)
    tool_call_id: str = ""
    current_role: str = "primary"  # primary / subagent
    is_subagent_call: bool = False
    is_team_member: bool = False


@dataclass
class PostToolUseEvent(HookEvent):
    """工具调用后触发"""

    tool_name: str = ""
    tool_result: Any = None
    tool_call_id: str = ""
    current_role: str = "primary"
    is_subagent_call: bool = False
    is_team_member: bool = False
    success: bool = True


@dataclass
class StopEvent(HookEvent):
    """停止流式输出触发"""

    reason: str = "completed"  # completed / cancelled / error
    is_team_member: bool = False


@dataclass
class PluginChangedEvent(HookEvent):
    """插件变更触发"""

    action: str = ""  # installed / updated / uninstalled / enabled / disabled / mcp_added ...
    plugin_name: str = ""
    diff: Dict[str, List[str]] = field(default_factory=dict)  # 工具/MCP 增删改明细
    sub_actions: List[str] = field(default_factory=list)


@dataclass
class TeamMailEvent(HookEvent):
    """团队邮件到达触发"""

    from_agent: str = ""
    from_window: str = ""
    subject: str = ""
    body: str = ""
    is_team_member: bool = True  # 团队邮件天然仅成员收到


# ============================================================
# 策略接口
# ============================================================


@runtime_checkable
class HookPolicy(Protocol):
    """Hook 触发策略接口（每域独立激活一个）"""

    id: str
    scope: str  # SCOPE_MAIN / SCOPE_SUBAGENT / SCOPE_TEAM_MEMBER

    def should_trigger(self, event: HookEvent) -> HookDecision:
        """判定给定事件是否应触发 hook 执行

        插件通过 isinstance(event, PreToolUseEvent) 等判定具体事件类型。
        """
        ...
