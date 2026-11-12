# -*- coding: utf-8 -*-
"""
PreUserMessage Hook 函数 — 将引用的历史会话内容注入 LLM 上下文

与 inject_command_prompt（命令/技能注入）同款机制：
1. main_widget 在 @ 卡片「会话」分类选中历史会话时，
   将 session_id 列表存入 session.metadata["_pending_session_refs"]
2. engine.py 在构建 PreUserMessage 上下文时，读取并 pop 该 metadata
3. 将 pending_session_refs 加入 pre_user_ctx → 传递给本 hook
4. 本 hook 从会话存储加载历史会话，只保留「用户问题 + 助手正式回答」
   （剔除工具调用与思考内容），格式化输出 → 自动注入 session.messages
"""


def hook(event: str, context: dict) -> str:
    """将引用历史会话的浓缩记录注入为 PreUserMessage hook 输出

    Args:
        event: 事件名称（PreUserMessage）
        context: 由 engine.py 构建的上下文，含：
            - pending_session_refs: list[str]（可选）引用的历史会话 id 列表

    Returns:
        格式化后的历史会话记录，或空字符串（无 pending 时）
    """
    refs = context.get("pending_session_refs")
    if not refs:
        return ""
    try:
        from app.core.context.session_reference import build_referenced_context

        return build_referenced_context(list(refs))
    except Exception:
        # 注入失败不阻塞发送（返回空 = 无 hook 输出）
        return ""
