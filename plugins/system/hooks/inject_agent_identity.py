# -*- coding: utf-8 -*-
"""
SessionStart Hook 函数 — 将团队成员分领智能体的提示词注入其会话

matcher="#team_member"（hooks.json 中与团队上下文同组）：
仅当窗口是团队成员时触发。按 context["window_id"] 在 TeamManager
成员表中定位自身角色（agent_name），再从 AgentManager 取该智能体的
prompt 注入——成员分领不同智能体后各自看到自己的身份定义。

非团队成员窗口：matcher 不命中，恒不注入。

prompt 为空时回退到与 get_agent_system_prompt() 一致的
"# 名称 + 描述 + Available Tools" 格式，保证身份块不缺内容。
"""

import logging

logger = logging.getLogger(__name__)


def _find_member_agent_name(tm, window_id: str) -> str:
    """根据窗口 ID 查找当前成员的角色名（agent_name）。"""
    try:
        members = tm.get_members()
        for m in members:
            if m.get("window_id") == window_id:
                return m.get("agent_name") or ""
    except Exception:
        pass
    return ""


def hook(event: str, context: dict) -> str:
    """输出当前成员分领智能体的身份提示词

    Args:
        event: 事件名称（SessionStart）
        context: 由 backend._build_session_context 构建的上下文，含：
            - is_team_member: bool 当前窗口是否是团队成员（matcher 已前置过滤）
            - window_id: str 当前窗口 ID（用于定位成员角色）

    Returns:
        智能体身份提示词，或空字符串（非团队成员 / 未分领智能体）
    """
    if not context.get("is_team_member"):
        return ""

    window_id = context.get("window_id", "") or ""
    if not window_id:
        return ""

    try:
        from app.core.agent import create_agent_manager
        from app.core.team_manager import TeamManager

        tm = TeamManager.get_instance()
        agent_name = _find_member_agent_name(tm, window_id)
        if not agent_name:
            return ""

        agent = create_agent_manager().get_agent(agent_name)
        if agent is None:
            logger.debug(f"[inject_agent_identity] 未找到智能体: {agent_name}")
            return ""

        content = (agent.prompt or "").strip()
        if not content:
            content = (
                f"# {agent.name}\n{agent.description}\n\n"
                "## Available Tools\nUse the tools available to you based on your permissions."
            )
        return f"当前智能体身份：{content}"
    except Exception as e:
        logger.warning(f"[inject_agent_identity] 注入失败: {e}")
        return ""
