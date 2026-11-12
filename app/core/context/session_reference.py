# -*- coding: utf-8 -*-
"""历史会话引用上下文构建

@ 卡片「会话」分类选中历史会话后，发送时由 system-hooks 的
inject_session_reference（PreUserMessage hook，与命令注入同款
metadata pending 机制）加载并注入上下文。

提取规则（用户要求）：
- 只保留「用户问题」与「大模型正式回答」；
- 剔除工具调用/工具结果、思考（thinking/reasoning）内容、hook 注入消息。
"""

from typing import Any, Dict, List

from loguru import logger

# 单条摘录上限（字符），超长截断；整体上限防止单次引用撑爆上下文
_MAX_ENTRY_CHARS = 1600
_MAX_SESSION_CHARS = 8000
_MAX_ROUNDS = 40


def _content_to_text(content: Any) -> str:
    """把消息 content（str 或多段 list）归一为纯文本

    多段 list 只保留 type=="text" 的段，思考/工具段一律丢弃。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for seg in content:
            if not isinstance(seg, dict):
                if isinstance(seg, str):
                    parts.append(seg)
                continue
            # 思考/推理/工具相关段一律剔除
            if str(seg.get("type", "text")).lower() not in ("text", ""):
                continue
            txt = seg.get("text", "")
            if isinstance(txt, str) and txt.strip():
                parts.append(txt)
        return "\n".join(parts)
    return ""


def _looks_like_tool_payload(msg: Dict[str, Any]) -> bool:
    """判断消息是否为工具调用/工具结果载体（整体剔除）"""
    if msg.get("tool_calls") or msg.get("tool_call_id"):
        return True
    role = str(msg.get("role", ""))
    return role in ("tool", "tool_result", "function")


def condense_history_session(session: Dict[str, Any]) -> str:
    """把一个历史会话 dict 浓缩为「用户问题 + 助手正式回答」对话记录文本"""
    if not isinstance(session, dict):
        return ""
    messages = session.get("messages") or []
    lines: List[str] = []
    total = 0
    rounds = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        # hook 注入消息（含历史引用本身）不是真实对话，剔除
        if msg.get("_hook_event"):
            continue
        if _looks_like_tool_payload(msg):
            continue
        role = str(msg.get("role", ""))
        if role not in ("user", "assistant"):
            continue
        text = _content_to_text(msg.get("content")).strip()
        if not text:
            continue
        if rounds >= _MAX_ROUNDS:
            lines.append("…（更早轮次已省略）")
            break
        rounds += 1
        if len(text) > _MAX_ENTRY_CHARS:
            text = text[:_MAX_ENTRY_CHARS] + " …"
        speaker = "用户" if role == "user" else "助手"
        lines.append(f"{speaker}: {text}")
        total += len(text)
        if total >= _MAX_SESSION_CHARS:
            lines.append("…（后续内容已省略）")
            break
    return "\n\n".join(lines)


def build_referenced_context(session_ids: List[str]) -> str:
    """构建引用历史会话的上下文文本（供 hook 注入）

    通过 ChatBackend.get_session_storage() 门面取会话；单个会话加载
    失败跳过不阻塞发送。
    """
    ids = [sid for sid in (session_ids or []) if sid]
    if not ids:
        return ""
    try:
        from app.core.conversation.backend import get_session_storage

        storage = get_session_storage()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[SessionReference] 获取会话存储失败: {e}")
        return ""
    if storage is None:
        return ""

    blocks: List[str] = []
    for sid in ids:
        try:
            data = storage.get(sid)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[SessionReference] 加载会话 {sid} 失败: {e}")
            continue
        if not data:
            continue
        title = str(data.get("title", "") or "未命名会话")
        body = condense_history_session(data)
        if not body:
            continue
        blocks.append(f"### 历史会话「{title}」\n\n{body}")
    if not blocks:
        return ""
    header = (
        "<referenced_sessions>\n"
        "以下是用户引用的历史会话记录（已剔除工具调用与思考过程，"
        "仅保留用户问题与助手最终回答），供本次对话参考：\n\n"
    )
    return header + "\n\n---\n\n".join(blocks) + "\n</referenced_sessions>"
