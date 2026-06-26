# -*- coding: utf-8 -*-
"""
PreUserMessage Hook 函数 — 将条目记忆、关键文档、worktree 上下文格式化为 LLM 提示

所有数据由 backend 预取后通过 context 传入，本函数不做任何文件 I/O，
仅负责格式化输出。

注意：PreUserMessage hook 会在每轮用户消息前执行，每次注入最新状态的
记忆和上下文到 session.messages 中。旧轮次的 hook 消息会在 round 截断时
被自动清理（由 get_user_round_ranges 控制 round 边界），因此虽然 session
中可能有多个 PreUserMessage 条目，但只有最近几轮才会保留在上下文中。

动态内容（worktree/分支信息/路径建议）从 SessionStart 移至此，
确保分支切换等中途操作后 LLM 能感知最新状态。
"""


def hook(event: str, context: dict) -> str:
    """将条目记忆、关键文档、worktree 信息格式化为字符串

    Args:
        event: 事件名称（PreUserMessage）
        context: 由 backend 预取的上下文，含：
            - entry_memories: list[str] 条目记忆列表
            - key_documents: list[dict] 关键文档列表
            - project_root: str 当前窗口工作目录
            - project_name: str 当前项目名
            - worktree: dict (可选) repo_name/current_branch/workdir/is_worktree/other_branches

    Returns:
        格式化的长期记忆 + 项目上下文字符串
    """
    entry_memories = context.get("entry_memories", [])
    key_documents = context.get("key_documents", [])
    project_root = context.get("project_root", "")
    project_name = context.get("project_name", "")
    worktree = context.get("worktree")

    parts = []

    # ====== 条目记忆 ======
    mem_lines = ["## 长期记忆", "", "### 条目记忆"]
    if entry_memories:
        for content in entry_memories:
            mem_lines.append(f"- {content}")
    else:
        mem_lines.append("- 暂无条目记忆")
    mem_lines.append("")

    # ====== 关键文档 ======
    mem_lines.append("### 关键文档")
    if key_documents:
        for doc in key_documents:
            file_name = doc.get("file_name", "")
            display = doc.get("display", "")
            is_url = doc.get("is_url", False)
            is_wd = doc.get("is_wd", False)
            if is_wd:
                mem_lines.append(f"- {file_name}（工作目录）")
            elif is_url:
                mem_lines.append(f"- 🔗 [{file_name}]({display})")
            else:
                mem_lines.append(f"- {file_name} ({display})")
    else:
        mem_lines.append("- 暂无关键文档")

    parts.append("\n".join(mem_lines))

    # ====== 项目上下文（动态，每次 PreUserMessage 更新） ======
    if project_root:
        ctx_lines = ["## 项目上下文", ""]
        ctx_lines.append(f"- 项目根目录: {project_root}")
        ctx_lines.append("- 根目录内：用相对路径（如 `src/main.py`），节省 token")
        ctx_lines.append("- 根目录外：用绝对路径")

        # Worktree / 分支信息
        if worktree:
            ctx_lines.append("")
            ctx_lines.append("### 当前 Worktree")
            ctx_lines.append(f"- 仓库: {worktree.get('repo_name', '')}")
            ctx_lines.append(f"- 当前分支: {worktree.get('current_branch', '')}")
            ctx_lines.append(f"- 工作目录: {worktree.get('workdir', project_root)}")
            if worktree.get("is_worktree"):
                ctx_lines.append("- ⚠️ 当前在 worktree 分支上工作，文件操作不影响主仓库代码")
            other_branches = worktree.get("other_branches", [])
            if other_branches:
                ctx_lines.append(f"- 其他分支: {', '.join(other_branches)}")

        parts.append("\n".join(ctx_lines))

    # 如果没有任何内容，返回空（让 backend 跳过注入）
    if not entry_memories and not key_documents and not project_root:
        return ""

    return "\n\n".join(parts)
