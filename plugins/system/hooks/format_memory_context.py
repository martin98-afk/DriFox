# -*- coding: utf-8 -*-
"""
PreUserMessage Hook 函数 — 将条目记忆与关键文档格式化为 LLM 提示

所有数据由 backend.build_memory_context_dict() 预取后通过 context 传入，
本函数不做任何文件 I/O 或方法调用，仅负责格式化输出。

注意：PreUserMessage hook 有内置去重机制（backend 中的 on_hook_finished
每次触发前会删除同类型的旧 hook 消息），因此上下文永远只保留最新一份。
"""


def hook(event: str, context: dict) -> str:
    """将条目记忆与关键文档格式化为字符串

    Args:
        event: 事件名称（PreUserMessage）
        context: 由 backend.build_memory_context_dict() 预取的上下文，含：
            - entry_memories: list[str] 条目记忆列表
            - key_documents: list[dict] 关键文档列表

    Returns:
        格式化的长期记忆字符串
    """
    entry_memories = context.get("entry_memories", [])
    key_documents = context.get("key_documents", [])

    if not entry_memories and not key_documents:
        return ""

    lines = ["## 长期记忆", ""]

    # 条目记忆
    lines.append("### 条目记忆")
    if entry_memories:
        for content in entry_memories:
            lines.append(f"- {content}")
    else:
        lines.append("- 暂无条目记忆")
    lines.append("")

    # 关键文档
    lines.append("### 关键文档")
    if key_documents:
        for doc in key_documents:
            file_name = doc.get("file_name", "")
            display = doc.get("display", "")
            is_url = doc.get("is_url", False)
            is_wd = doc.get("is_wd", False)
            if is_wd:
                lines.append(f"- {file_name} （项目根目录）./")
            elif is_url:
                lines.append(f"- 🔗 [{file_name}]({display})")
            else:
                lines.append(f"- {file_name} ({display})")
    else:
        lines.append("- 暂无关键文档")

    return "\n".join(lines)
