# -*- coding: utf-8 -*-
"""
PreUserMessage Hook 函数 — 将命令/技能提示词注入 LLM 上下文

当用户在命令卡片或技能面板中选中 PROMPT/AGENT 类型命令或技能时，
此 hook 从 context 中读取 main_widget 存下的 pending 信息，将命令/技能
提示词格式化为 hook 输出注入到 session.messages 中，而不直接修改用户消息本体。

工作流程：
1. main_widget._on_send_clicked 检测到 PROMPT/AGENT 命令或技能
2. 将 {prompt_text, command_name} 存入 session.metadata["_pending_command"]
   或将 {name, content, ...} 存入 session.metadata["_pending_skill"]
3. engine.py 在构建 PreUserMessage 上下文时，读取并 pop 该 metadata
4. 将 pending 信息加入 pre_user_ctx → 传递给本 hook
5. 本 hook 输出格式化后的提示词 → 自动注入 session.messages
"""


def hook(event: str, context: dict) -> str:
    """将命令/技能提示词注入为 PreUserMessage hook 输出

    Args:
        event: 事件名称（PreUserMessage）
        context: 由 engine.py 构建的上下文，含：
            - pending_command: dict（可选）命令信息
                - prompt_text: str 命令的提示词
                - command_name: str 命令名
                - remainder: str 命令后的参数
            - pending_skill: dict（可选）技能信息
                - name: str 技能名
                - content: str 技能内容（SKILL.md）
                - workspace: str 技能工作目录
                - remainder: str 技能后的参数
            - message: str 当前用户消息

    Returns:
        格式化后的提示词字符串，或空字符串（无 pending 时）
    """
    # ─── 命令注入 ─────────────────────────────────────────────
    pending = context.get("pending_command")
    if pending:
        return _format_command_output(pending)

    # ─── 技能注入 ─────────────────────────────────────────────
    pending_skill = context.get("pending_skill")
    if pending_skill:
        return _format_skill_output(pending_skill)

    return ""


def _format_command_output(pending: dict) -> str:
    """格式化命令提示词输出"""
    prompt_text = pending.get("prompt_text", "")
    command_name = pending.get("command_name", "")
    remainder = (pending.get("remainder") or "").strip()

    if not prompt_text:
        return ""

    args_text = remainder if remainder else "无用户参数"

    return (
        f"用户通过 /{command_name} 命令发起了请求，请严格按照以下命令规范执行：\n"
        f"---\n"
        f"{prompt_text}\n"
        f"---\n"
        f"$ARGUMENTS：{args_text}"
    )


def _format_skill_output(pending: dict) -> str:
    """格式化技能提示词输出"""
    name = pending.get("name", "")
    content = pending.get("content", "")
    workspace = pending.get("workspace", "")
    remainder = (pending.get("remainder") or "").strip()

    if not content:
        return ""

    parts = [f"用户通过 /{name} 技能发起了请求，已自动加载该技能："]

    if workspace:
        parts.append(f"\n技能工作目录：{workspace}")

    parts.append(f"\n--- 技能：{name} ---")
    parts.append(content)
    parts.append("--- 技能结束 ---")

    if remainder:
        parts.append(f"\n$ARGUMENTS：{remainder}")

    return "".join(parts)
