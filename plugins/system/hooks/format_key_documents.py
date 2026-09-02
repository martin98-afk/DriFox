# -*- coding: utf-8 -*-
"""format_key_documents.py — PreUserMessage Hook：注入关键文档列表

所有数据由 backend 预取（build_key_documents_context，SQLite）后通过 context
传入，本函数不做任何 I/O，仅负责格式化。

关键文档是用户显式登记的项目入口（工作目录/参考文件/URL），人工维护，
每轮用户消息前注入最新状态。
"""

from __future__ import annotations

from typing import Any, Dict


def hook(event: str, context: dict) -> str:
    """注入关键文档列表

    Args:
        event: 事件名称（PreUserMessage）
        context: 由 backend 预取的上下文，含：
            - key_documents: list[dict] 关键文档列表（file_name/display/is_url/is_wd）

    Returns:
        格式化的关键文档字符串；无文档时返回空串（不注入空块）
    """
    key_documents: list[Dict[str, Any]] = context.get("key_documents") or []
    if not key_documents:
        return ""

    mem_lines = ["### 关键文档"]
    for doc in key_documents:
        file_name = doc.get("file_name", "")
        display = doc.get("display", "")
        is_url = doc.get("is_url", False)
        is_wd = doc.get("is_wd", False)
        if is_wd:
            wd_display = display or file_name
            mem_lines.append(f"- {file_name}（工作目录: {wd_display}）")
        elif is_url:
            mem_lines.append(f"- 🔗 [{file_name}]({display})")
        else:
            mem_lines.append(f"- {file_name} ({display})")

    return "\n".join(mem_lines)
