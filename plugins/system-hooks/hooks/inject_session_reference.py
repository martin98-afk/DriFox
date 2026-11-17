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
    from loguru import logger

    refs = context.get("pending_session_refs")
    if not refs:
        # 无 pending（用户未引用会话）属正常路径，debug 留痕即可
        logger.debug("[SessionReference] 无 pending_session_refs，跳过引用注入")
        return ""
    try:
        from app.core.context.session_reference import build_referenced_context

        output = build_referenced_context(list(refs))
        if not output:
            # refs 有值但构建为空：会话加载失败或浓缩后无有效内容，
            # 静默返回会让「引用了却没注入」无迹可查（2026-11-17 排查教训）
            logger.warning(f"[SessionReference] 引用会话注入为空: refs={list(refs)}（会话不存在或浓缩后无有效内容）")
        return output
    except Exception:
        # 注入失败不阻塞发送（返回空 = 无 hook 输出），但必须留痕
        logger.exception(f"[SessionReference] 引用会话注入失败: refs={list(refs)}")
        return ""
