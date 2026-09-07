# -*- coding: utf-8 -*-
"""
BuildSystemPrompt Hook 函数 — 将用户启用的技能内容注入系统提示词

所有数据由 get_agent_system_prompt() 预取后通过 context 传入，
本函数不做任何 import 或 I/O，仅负责格式化输出。

替代了原 context_builder.py 中硬编码的技能注入逻辑，用户可自由替换/禁用此 hook。
"""


def hook(event: str, context: dict) -> str:
    """从 context 取出预取好的技能内容

    Args:
        event: 事件名称（BuildSystemPrompt）
        context: 由 get_agent_system_prompt() 预取的上下文，含：
            - enabled_skills_content: str 预先格式化好的技能列表

    Returns:
        预取好的技能内容字符串，或空字符串
    """
    return context.get("enabled_skills_content", "")
