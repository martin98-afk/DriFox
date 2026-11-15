# -*- coding: utf-8 -*-
"""
Hook 函数 — 注入可用子智能体清单（仅 SessionStart）

清单只在会话启动时注入一次进会话消息，不占 system prompt。
子智能体清单只对主对话有意义：子智能体自身不能发布子任务（subagent_para 被
禁用），Gateway 也禁用 subagent_para，两者都不需要这份清单。

数据由本插件经能力池自取，主程序不做预取：``agent.read`` 拿管理器，
按其 list_subagents 口径列举（含 hidden）。描述截断 300 字，标题行格式与
设置页 token 估算（app/widgets/cards/settings/plugin_components_card.py）一致。
"""

from loguru import logger

def _build_block() -> str:
    """按 ## Available Subagents 格式拼清单；无子智能体返回空串。"""
    try:
        from app.plugins.sdk import caps_for

        manager = caps_for("agent.read", surface="ui")
    except Exception as e:
        logger.debug(f"[inject_subagent_info] 取智能体管理器失败: {e}")
        return ""
    if manager is None:
        return ""

    try:
        agents = manager.list_subagents(include_hidden=True) or []
    except Exception as e:
        logger.debug(f"[inject_subagent_info] 列举子智能体失败: {e}")
        return ""
    if not agents:
        return ""

    lines = ["## Available Subagents\n可直接使用的子智能体列表(可供subagent_para使用)："]
    for agent in agents:
        description = str(getattr(agent, "description", "") or "")[:300]
        lines.append(f"- **{agent.name}**: {description}")
    return "\n".join(lines)

def hook(event: str, context: dict) -> str:
    """输出 <available_subagents> 包裹的子智能体清单

    Args:
        event: 事件名称（仅 SessionStart 声明，其他事件直接返回空）

    Returns:
        <available_subagents> 包裹的列表字符串，或空字符串
    """
    if event != "SessionStart":
        return ""
    content = _build_block()
    if not content:
        return ""
    return f"<available_subagents>\n{content}\n</available_subagents>"
