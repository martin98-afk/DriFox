# -*- coding: utf-8 -*-
"""
SessionStart Hook 函数 — 将团队模板描述 + 当前成员角色描述注入团队成员会话

当窗口是团队成员（hooks.json 中 matcher="#team_member"）时，从 TeamManager
读取当前团队的模板上下文（/team --load=<name> 加载模板时写入），按成员各自注入：

1. 模板描述（template.description）—— 团队整体上下文
2. 当前成员自己的角色描述（agents 列表中与自身 agent_name 匹配的 description）

按成员各自注入（不是广播全量角色列表）：每个成员只收到自己角色的描述，
避免暴露团队全部成员信息、减少上下文占用。角色描述为空时不追加。

设计要点：
- 依赖 context["window_id"]（backend._build_session_context 注入），
  用于在 TeamManager 中定位当前窗口对应的 agent_name
- 旧版模板 agents 条目可能是不带 description 的字符串，或 {agent_name: ...} 结构，
  一律兼容：找不到描述时不注入角色段落
"""

import re

# 用户自建模板（/team --save=）自动生成的描述："由 N 个活跃窗口保存（去重 M 个角色）"
_MEANINGLESS_DESC_PATTERN = re.compile(r"^由\s*\d+\s*个活跃窗口保存")


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


def _find_agent_description(agents, agent_name: str) -> str:
    """从模板 agents 列表中找到指定角色的描述（兼容字符串 / dict 两种旧格式）。"""
    if not agent_name:
        return ""
    for item in agents or []:
        if isinstance(item, dict):
            if item.get("agent_name") == agent_name:
                return str(item.get("description") or "").strip()
        elif isinstance(item, str) and item == agent_name:
            return ""
    return ""


def hook(event: str, context: dict) -> str:
    """将团队模板描述 + 当前成员角色描述注入为 SessionStart hook 输出

    Args:
        event: 事件名称（SessionStart）
        context: 由 backend._build_session_context 构建的上下文，含：
            - is_team_member: bool 当前窗口是否是团队成员
            - window_id: str 当前窗口 ID（用于定位成员角色）

    Returns:
        团队描述 + 角色描述文本，或空字符串（非团队成员 / 无模板）
    """
    if not context.get("is_team_member"):
        return ""

    try:
        from app.core.team_manager import TeamManager

        tm = TeamManager.get_instance()
        template = tm.get_template()
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

    # 按成员各自注入：定位当前窗口的角色，附加其角色描述
    window_id = context.get("window_id", "") or ""
    agent_name = _find_member_agent_name(tm, window_id)
    if agent_name:
        role_desc = _find_agent_description(template.get("agents"), agent_name)
        if role_desc:
            parts.append(f"你的角色「{agent_name}」：{role_desc}")

    # 🛡️ M1：多团队并存时追加团队间沟通规则——
    # 1) 避免各成员误向其他团队成员发件；2) 提示经各自 leader 转发。
    try:
        run_ids = tm.get_team_run_ids()
    except Exception:
        run_ids = []
    if len(run_ids) > 1:
        labels: list = []
        for rid in run_ids:
            try:
                labels.append(tm.get_team_label_by_run(rid) or rid)
            except Exception:
                labels.append(rid)
        labels_text = "、".join(f"「{l}」" for l in labels)
        parts.append(
            f"⚠ 多团队并存：本系统有 {len(run_ids)} 个团队同时运行（{labels_text}）。"
            f"团队间沟通规则：你只能向本团队成员发送任务；"
            f"如需联系其他团队，只通过各自 leader 传递团队间消息。"
        )

    return "\n".join(parts)
