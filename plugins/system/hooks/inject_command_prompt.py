# -*- coding: utf-8 -*-
"""
PreUserMessage Hook 函数 — 将命令卡片选中的命令提示词注入 LLM 上下文

当用户在命令卡片中选中 PROMPT/AGENT 类型命令时，此 hook 从 context 中读取
main_widget 存下的 pending_command 信息，将命令提示词格式化为 hook 输出注入
到 session.messages 中，而不直接修改用户消息本体。

工作流程：
1. main_widget._on_send_clicked 检测到 PROMPT/AGENT 命令
2. 将 {prompt_text, command_name} 存入 session.metadata["_pending_command"]
3. engine.py 在构建 PreUserMessage 上下文时，读取并 pop 该 metadata
4. 将 pending_command 加入 pre_user_ctx → 传递给本 hook
5. 本 hook 输出格式化后的命令提示词 → 自动注入 session.messages
"""


def hook(event: str, context: dict) -> str:
    """将命令提示词注入为 PreUserMessage hook 输出

    Args:
        event: 事件名称（PreUserMessage）
        context: 由 engine.py 构建的上下文，含：
            - pending_command: dict（可选）
                - prompt_text: str 命令的提示词（已按 prompt_sections 过滤）
                - command_name: str 命令名
            - message: str 当前用户消息（即命令后的参数部分）

    Returns:
        格式化后的命令提示词字符串，或空字符串（无 pending_command 时）
    """
    pending = context.get("pending_command")
    if not pending:
        return ""

    prompt_text = pending.get("prompt_text", "")
    command_name = pending.get("command_name", "")
    # 使用 remainder（命令后的参数部分），而非完整 user_text（含 /xxx 前缀）
    remainder = (pending.get("remainder") or "").strip()

    if not prompt_text:
        return ""

    # 构建参数描述（保留原始用户输入或标记无参数）
    args_text = remainder if remainder else "无用户参数"

    return (
        f"用户通过 /{command_name} 命令发起了请求，请严格按照以下命令规范执行：\n"
        f"---\n"
        f"{prompt_text}\n"
        f"---\n"
        f"$ARGUMENTS：{args_text}"
    )
