# -*- coding: utf-8 -*-
"""
BuildSystemPrompt Hook 函数 — 为主智能体注入可用子智能体列表

所有数据由 get_agent_system_prompt() 预取后通过 context 传入，
本函数不做任何 import 或 I/O，仅负责格式化输出。

仅对主智能体（current_role="primary"）生效，matcher="primary" 在 hooks.json 中控制。
替代了原 agent.py get_agent_system_prompt() 中硬编码的 get_available_subagents_for_prompt() 调用。
"""


def hook(event: str, context: dict) -> str:
    """从 context 取出预取好的子智能体列表

    Args:
        event: 事件名称（BuildSystemPrompt）
        context: 由 get_agent_system_prompt() 预取的上下文，含：
            - available_subagents_content: str 预先格式化好的子智能体列表

    Returns:
        预取好的子智能体列表字符串，或空字符串
    """
    return context.get("available_subagents_content", "")
