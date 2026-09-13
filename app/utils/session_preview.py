# -*- coding: utf-8 -*-
"""会话列表展示辅助（相对时间 / 消息预览）

原本定义在 ``app/widgets/cards/settings/history_card.py`` 内，被历史卡片之外的
模块（消息卡片时间戳、归档预览）复用。历史卡片已迁移到 ``history-manager`` 插件，
故这两个**通用**辅助上移到 ``app/utils/``，避免主程序或插件反向依赖某个具体卡片的
实现模块。

约定：不依赖任何 Qt / 卡片模块，纯函数，便于单测。
"""

import datetime
from typing import Dict, List


def format_relative_time(time_str: str) -> str:
    """将时间字符串（``%Y-%m-%d %H:%M:%S``）转换为相对时间显示"""
    if not time_str or time_str == "未知":
        return "更早"
    try:
        session_time = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        now = datetime.datetime.now()
        diff = now - session_time

        if diff.total_seconds() < 60:
            return "刚刚"
        elif diff.total_seconds() < 3600:
            minutes = int(diff.total_seconds() / 60)
            return f"{minutes}分钟前"
        elif diff.total_seconds() < 86400:
            hours = int(diff.total_seconds() / 3600)
            return f"{hours}小时前"
        elif diff.days == 1:
            return "昨天"
        elif diff.days < 7:
            return f"{diff.days}天前"
        else:
            return time_str[5:10] if len(time_str) >= 10 else time_str
    except (ValueError, TypeError):
        return time_str[5:10] if time_str and len(time_str) >= 10 else "更早"


def get_message_preview(messages: List[Dict], max_len: int = 50) -> str:
    """从消息列表中提取预览文本（取最后一条真实 user 消息）

    跳过 hook 注入消息（``role="user"`` 但带 ``_hook_event`` 标记），避免预览
    显示 hook 内容。
    """
    if not messages:
        return ""
    for msg in reversed(messages):
        role = msg.get("role", "")
        content = msg.get("content", "")
        # 跳过 hook 消息（role=user 但带 _hook_event 标记），避免预览显示 hook 内容
        if msg.get("_hook_event"):
            continue
        if role == "user" and content:
            if isinstance(content, list):
                content = " ".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
            return content[:max_len].strip() + ("..." if len(content) > max_len else "")
    return ""
