# -*- coding: utf-8 -*-
"""内置上下文降级层实现（order 10-80）。

每层独立文件，便于插件按 id 精准覆盖单层。
层实现体尽量复用 history_compactor / tool_prune 的既有函数（行为零变化），
tier 只负责 trigger 判定与结果包装。
"""

from __future__ import annotations

from typing import Any, Dict, List


def count_tokens(messages: List[Dict[str, Any]]) -> int:
    """消息列表 token 估算；异常时返回 0（tier 不得因估算失败而崩）。"""
    from app.core.token_estimator import count_messages_tokens

    try:
        return count_messages_tokens(messages)
    except Exception:
        return 0
