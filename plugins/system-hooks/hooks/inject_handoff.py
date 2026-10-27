# -*- coding: utf-8 -*-
"""
SessionStart Hook 函数 — 注入交接文档

只在 backend 的 `_pending_handoff` 置位时被调用（handoff 切会话路径）。
ctx["handoff"] 缺省时返回空字符串，用户手动新建会话不产生任何注入。

前缀语义参考 Codex CLI 的 summary_prefix：明确告诉新模型「上一任留下了交接，
已完成的工作不必重做」，避免它把交接当成用户的新需求重新分析一遍。
"""

_PREFIX = """另一个语言模型此前在处理同一个任务，留下了这份交接文档。它使用的工具与你共享，
已完成的工作不需要重做。基于下面的交接继续推进；交接里写明的用户偏好与约束同样对你有效。

"""


def hook(event: str, context: dict) -> str:
    """输出交接文档（前缀 + 正文）

    Args:
        event: 事件名称（SessionStart）
        context: 由 backend._build_session_context 构建，含：
            - handoff: dict，{"path" str, "content" str, "source_session_id" str}

    Returns:
        前缀 + 交接正文；无待注入交接时返回空字符串
    """
    payload = (context or {}).get("handoff") or {}
    content = str(payload.get("content") or "").strip()
    if not content:
        return ""

    source = str(payload.get("source_session_id") or "").strip()
    tail = f"\n\n（源会话 ID：{source}，可在历史面板回查原始对话）" if source else ""
    return _PREFIX + content + tail
