# -*- coding: utf-8 -*-
"""
SessionStart Hook 函数 — 将团队模板描述注入团队成员会话

当窗口是团队成员（hooks.json 中 matcher="#team_member"）时，从 TeamManager
读取当前团队的模板上下文（/team --load=<name> 加载模板时写入），格式化后注入。

按描述内容判断（成员列表不注入——团队成员可能变更，SessionStart 注入后
整个会话固定会过期；AI 需要最新成员时用 team_list_members() 动态查询）：
- description 有实际内容（系统内置模板人工编写）→ 注入团队描述
- description 为空或自动生成（用户自建模板："由 N 个窗口保存..."）→ 不注入
"""

import re

# 用户自建模板（/team --save=）自动生成的描述："由 N 个活跃窗口保存（去重 M 个角色）"
_MEANINGLESS_DESC_PATTERN = re.compile(r"^由\s*\d+\s*个活跃窗口保存")


def hook(event: str, context: dict) -> str:
    """将团队模板描述注入为 SessionStart hook 输出

    Args:
        event: 事件名称（SessionStart）
        context: 由 backend._build_session_context 构建的上下文，含：
            - is_team_member: bool 当前窗口是否是团队成员

    Returns:
        团队描述文本，或空字符串（非团队成员 / 无模板 / 描述无实际内容）
    """
    if not context.get("is_team_member"):
        return ""

    try:
        from app.core.team_manager import TeamManager

        template = TeamManager.get_instance().get_template()
    except Exception:
        return ""

    if not template:
        return ""

    description = (template.get("description") or "").strip()
    # 无实际内容（空 / 用户自建模板的自动生成描述）→ 不注入
    if not description or _MEANINGLESS_DESC_PATTERN.match(description):
        return ""

    name = (template.get("name") or "").strip()
    parts = []
    if name:
        parts.append(f"团队「{name}」协作上下文")
    parts.append(description)
    return "\n".join(parts)
