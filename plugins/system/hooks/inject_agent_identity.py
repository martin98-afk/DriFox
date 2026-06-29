# -*- coding: utf-8 -*-
"""
BuildSystemPrompt Hook 函数 — 将主智能体身份定义注入系统提示词

由 get_agent_system_prompt() 预取 agent.prompt 后通过 context 传入，
本函数不做任何 import 或 I/O，仅负责格式化输出。

仅对主智能体（current_role="primary"）生效，matcher="primary" 在 hooks.json 中控制。
替代了原 agent.py get_agent_system_prompt() 中硬编码的 base = agent.prompt 逻辑。
"""


def hook(event: str, context: dict) -> str:
    """从 context 取出预取好的智能体身份内容

    Args:
        event: 事件名称（BuildSystemPrompt）
        context: 由 get_agent_system_prompt() 预取的上下文，含：
            - agent_identity_content: str 预先格式化好的智能体身份提示词

    Returns:
        预取好的智能体身份内容字符串，或空字符串
    """
    return context.get("agent_identity_content", "")
