# -*- coding: utf-8 -*-
"""
SessionStart Hook 函数 — 将预取的项目笔记（AGENTS.md）格式化为 LLM 提示

所有数据由 backend.py _build_session_context() 预取后通过 context 传入，
本函数不做任何文件 I/O 或方法调用，仅负责格式化输出。

注意：worktree 信息和路径使用建议已移至 PreUserMessage
（format_memory_context.py），因其内容可能随分支切换发生变化。
"""

def hook(event: str, context: dict) -> str:
    """将预取的项目笔记格式化为字符串

    Args:
        event: 事件名称（SessionStart）
        context: 由 _build_session_context() 预取的上下文，含：
            - project_root/project_name/project_notes_content
            - state: 会话状态（startup/resume/clear/compact）

    Returns:
        格式化的项目笔字符串
    """
    project_root = context.get("project_root", "")
    project_name = context.get("project_name", "")
    if not project_root:
        return "（未设置项目目录）"

    # 仅输出项目笔记（AGENTS.md 内容）
    notes_content = context.get("project_notes_content")
    if notes_content:
        return f"## 项目笔记\n[当前项目: {project_name}]\n{notes_content}"
    else:
        return f"## 项目笔记\n[当前项目: {project_name}]\n（项目笔记为空）"
