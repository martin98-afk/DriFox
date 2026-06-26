# -*- coding: utf-8 -*-
"""
SessionStart Hook 函数 — 将预取的项目上下文格式化为 LLM 提示

所有数据由 backend.py _build_session_context() 预取后通过 context 传入，
本函数不做任何文件 I/O 或方法调用，仅负责格式化输出。

包含三部分：
1. 项目笔记（AGENTS.md）
2. 路径使用建议
3. 当前 Worktree / git 分支信息
"""

def hook(event: str, context: dict) -> str:
    """将预取的项目上下文格式化为字符串

    Args:
        event: 事件名称（SessionStart）
        context: 由 _build_session_context() 预取的上下文，含：
            - project_root/project_name/project_notes_content
            - worktree (dict with repo_name/current_branch/etc.)

    Returns:
        格式化的项目上下文字符串
    """
    project_root = context.get("project_root", "")
    project_name = context.get("project_name", "")
    if not project_root:
        return "（未设置项目目录）"

    parts = []

    # ====== 1. 项目笔记（数据由 backend 预取，不直接读文件） ======
    notes_content = context.get("project_notes_content")
    if notes_content:
        parts.append(f"## 项目笔记\n[当前项目: {project_name}]\n{notes_content}")
    else:
        parts.append("（项目笔记为空）")

    # ====== 2. 路径使用建议 ======
    parts.append(
        "### 路径使用建议\n"
        f"- 项目根目录: {project_root}\n"
        "- 根目录内：用相对路径（如 `src/main.py`），节省 token\n"
        "- 根目录外：用绝对路径"
    )

    # ====== 3. Worktree 上下文（数据由 backend 预取，不调方法查） ======
    wt = context.get("worktree")
    if wt:
        wt_lines = ["### 当前 Worktree"]
        wt_lines.append(f"- 仓库: {wt.get('repo_name', '')}")
        wt_lines.append(f"- 当前分支: {wt.get('current_branch', '')}")
        wt_lines.append(f"- 工作目录: {wt.get('workdir', project_root)}")
        if wt.get("is_worktree"):
            wt_lines.append("- ⚠️ 当前在 worktree 分支上工作，文件操作不影响主仓库代码")
        other_branches = wt.get("other_branches", [])
        if other_branches:
            wt_lines.append(f"- 其他分支: {', '.join(other_branches)}")
        parts.append("\n".join(wt_lines))

    return "\n\n".join(parts)
