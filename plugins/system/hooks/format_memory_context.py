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

项目上下文段优先复用 Git 状态输出（与原 .drifox/plugins/git-status 同款格式）：
    - 项目已是 git 仓库 → 直接采集状态
    - 项目不是 git 仓库但系统装了 git → 自动 git init + 空 commit，再采集
    - git 未安装 / git init 失败 → fallback 到 backend 预取的 worktree 字段
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

# ============================================================
# Git 状态收集（移植自 .drifox/plugins/git-status/hooks/git_status.py）
# ============================================================

_GIT_TIMEOUT = 3
_MAX_CONTEXT_LENGTH = 2000
_MAX_STAGED_ITEMS = 30
_MAX_UNSTAGED_ITEMS = 30
_MAX_UNTRACKED_ITEMS = 20
_MAX_RECENT_COMMITS = 5
# 同一 cwd 只尝试一次自动 git init，避免每条用户消息都触发破坏性操作
_AUTO_INITED: set[str] = set()


def _run_git(cwd: str, *args: str) -> tuple[str, str, int]:
    """执行 git 命令并返回 (stdout, stderr, returncode)

    所有异常都被捕获并转为 returncode=-1，不抛错中断流程。
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        # 只去末尾换行符，保留前导空格（porcelain X=' ' 是有效信息）
        return result.stdout.rstrip("\n"), result.stderr.rstrip("\n"), result.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", -1
    except FileNotFoundError:
        return "", "git not found", -1
    except Exception:
        return "", "error", -1


def _is_git_available() -> bool:
    """检查系统是否安装了 git（cwd='.' 不依赖具体目录）"""
    _, _, code = _run_git(".", "--version")
    return code == 0


def _is_git_repo(cwd: str) -> bool:
    """检查目录是否在 Git 仓库中（含子目录）"""
    if not cwd:
        return False
    path = Path(cwd)
    if not path.exists() or not path.is_dir():
        return False
    _, _, code = _run_git(cwd, "rev-parse", "--is-inside-work-tree")
    return code == 0


def _auto_git_init(cwd: str) -> bool:
    """若项目不是 git 仓库但系统装了 git，则自动 git init + 空 commit

    用于让 model 在非 git 项目里也能感知到默认分支名（init 后立刻有 main/master）。

    Returns:
        True  → 项目已经是 git 仓库（无论是否本次 init）
        False → 没有 git 或 init 失败
    """
    if _is_git_repo(cwd):
        return True
    if not _is_git_available():
        return False
    # 同一目录只 init 一次，避免每轮用户消息都触发
    norm = str(Path(cwd).resolve())
    if norm in _AUTO_INITED:
        return _is_git_repo(cwd)
    _AUTO_INITED.add(norm)

    _, _, code = _run_git(cwd, "init")
    if code != 0:
        return False
    # 空 commit 让默认分支立即产生（否则 branch --show-current 返回空）
    _run_git(cwd, "commit", "--allow-empty", "-m", "init")
    return _is_git_repo(cwd)


def _collect_branch_info(cwd: str) -> dict[str, Any]:
    """收集分支名 + ahead/behind 提交数"""
    info: dict[str, Any] = {"branch": "", "ahead": 0, "behind": 0}
    stdout, _, code = _run_git(cwd, "branch", "--show-current")
    if code == 0 and stdout:
        info["branch"] = stdout
    else:
        # detached HEAD 场景
        stdout, _, code = _run_git(cwd, "rev-parse", "--short", "HEAD")
        if code == 0 and stdout:
            info["branch"] = f"(detached @ {stdout})"
    stdout, _, code = _run_git(cwd, "rev-list", "--left-right", "--count", "HEAD...@{u}")
    if code == 0 and stdout:
        parts = stdout.split()
        if len(parts) == 2:
            try:
                info["ahead"] = int(parts[0])
                info["behind"] = int(parts[1])
            except ValueError:
                pass
    return info


def _collect_file_status(cwd: str) -> dict[str, list]:
    """收集工作树文件状态（porcelain v1）"""
    status: dict[str, list] = {"staged": [], "unstaged": [], "untracked": []}
    stdout, _, code = _run_git(
        cwd,
        "status",
        "--porcelain=v1",
        "-uall",
        "--no-renames",
    )
    if code != 0 or not stdout:
        return status
    for line in stdout.splitlines():
        if len(line) < 3:
            continue
        x = line[0]
        y = line[1]
        path = line[3:]
        if x == "?" and y == "?":
            status["untracked"].append(path)
        else:
            if x != " ":
                status["staged"].append((x, path))
            if y != " ":
                status["unstaged"].append((y, path))
    return status


def _collect_diff_stats(cwd: str) -> dict[str, tuple[int, int]]:
    """每个文件的 diff 行数（numstat → {path: (added, removed)}）

    替代 --shortstat 聚合输出，让 LLM 能直接看到每个文件的改动规模，
    避免出现"250 行变化但不知道是哪个文件"的信息黑洞。

    二进制文件返回 (0, 0)。
    """
    stdout, _, code = _run_git(cwd, "diff", "--numstat")
    if code != 0 or not stdout:
        return {}
    stats: dict[str, tuple[int, int]] = {}
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if added == "-" and removed == "-":
            stats[path] = (0, 0)  # 二进制文件
        else:
            try:
                stats[path] = (int(added), int(removed))
            except ValueError:
                continue
    return stats


def _collect_stash_count(cwd: str) -> int:
    """stash 数量（git stash list）"""
    stdout, _, code = _run_git(cwd, "stash", "list")
    if code != 0 or not stdout:
        return 0
    return len(stdout.splitlines())


def _collect_recent_commits(cwd: str, n: int = _MAX_RECENT_COMMITS) -> list[str]:
    """最近 N 条 commit（短 hash + subject + 相对时间）

    %cr 输出形如 "3 minutes ago" / "2 days ago"，帮助 AI 判断活跃度。
    """
    stdout, _, code = _run_git(cwd, "log", f"-n{n}", "--pretty=format:%h %s (%cr)")
    if code == 0 and stdout:
        return stdout.splitlines()
    return []


_STATUS_CODE_DESC = {
    "M": "修改",
    "A": "新增",
    "D": "删除",
    "R": "改名",
    "C": "复制",
    "U": "未合并",
    "T": "类型变更",
}


def _describe_status(code: str) -> str:
    return _STATUS_CODE_DESC.get(code, code)


def _format_branch(info: dict[str, Any]) -> str:
    branch = info["branch"] or "(未知)"
    ahead = f" ↑{info['ahead']}" if info["ahead"] else ""
    behind = f" ↓{info['behind']}" if info["behind"] else ""
    return f"**当前分支**: `{branch}`{ahead}{behind}"


def _format_file_list(
    items: list,
    max_items: int,
    label: str,
    diff_stats: dict[str, tuple[int, int]] | None = None,
) -> list[str]:
    """格式化文件列表，可选附加每文件 diff 行数 (Opt 1: 内联变更统计)"""
    if not items:
        return []
    lines = [f"- {label} ({len(items)}):"]
    shown, overflow = items[:max_items], max(0, len(items) - max_items)
    for entry in shown:
        if isinstance(entry, tuple):
            code, path = entry
            extra = ""
            if diff_stats and path in diff_stats:
                added, removed = diff_stats[path]
                if added == 0 and removed == 0:
                    extra = " [二进制]"
                elif added or removed:
                    extra = f" (+{added}/-{removed})"
            lines.append(f"  - [{_describe_status(code)}] `{path}`{extra}")
        else:
            lines.append(f"  - `{entry}`")
    if overflow:
        lines.append(f"  - ... 还有 {overflow} 项")
    return lines


def _format_status_section(
    files: dict[str, list],
    diff_stats: dict[str, tuple[int, int]] | None = None,
) -> list[str]:
    staged = _format_file_list(files["staged"], _MAX_STAGED_ITEMS, "已暂存", diff_stats)
    unstaged = _format_file_list(files["unstaged"], _MAX_UNSTAGED_ITEMS, "未暂存", diff_stats)
    untracked = _format_file_list(files["untracked"], _MAX_UNTRACKED_ITEMS, "未跟踪")
    if not (staged or unstaged or untracked):
        return ["**工作树状态**: 工作树干净，无未提交修改 ✓"]
    return ["**工作树状态**:"] + staged + unstaged + untracked


def _format_recent_commits(commits: list[str]) -> str:
    if not commits:
        return "**最近 commits**: (无)"
    lines = ["**最近 commits**:"]
    for c in commits:
        lines.append(f"- `{c}`")
    return "\n".join(lines)


# 疑似临时调试文件命名模式（Opt 4: 脏文件提示）
_TEMP_FILE_PATTERNS = (
    "_diag",
    ".diag",
    ".tmp",
    ".bak",
    ".swp",
    ".swo",
    "debug_",
    "scratch_",
    "test_",
)

# .gitignore 自动生成内容（按 section 分组；缺失时整段追加）
_GITIGNORE_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Python 生态",
        (
            "__pycache__/",
            "*.pyc",
            "*.pyo",
            "*.pyd",
            ".venv/",
            "venv/",
            "*.egg-info/",
            "*.egg",
            "build/",
            "dist/",
            "*.spec",
            ".pytest_cache/",
            ".mypy_cache/",
            ".ruff_cache/",
            ".coverage",
            "htmlcov/",
        ),
    ),
    (
        "系统文件",
        (
            ".DS_Store",
            "Thumbs.db",
            "desktop.ini",
            "$RECYCLE.BIN/",
        ),
    ),
    (
        "IDE/编辑器",
        (
            ".idea/",
            ".vscode/",
            "*.swp",
            "*.swo",
            "*.swn",
            "*~",
            ".project",
            ".pycpath",
        ),
    )
)
# 同一 cwd 只处理一次 .gitignore（避免每轮用户消息都写文件）
_GITIGNORE_UPDATED: set[str] = set()


def _detect_temp_files(untracked: list[str]) -> list[str]:
    """识别疑似临时调试文件，避免污染工作树提示"""
    matched: list[str] = []
    for path in untracked:
        basename = Path(path).name.lower()
        for pattern in _TEMP_FILE_PATTERNS:
            if pattern in basename:
                matched.append(path)
                break
    return matched


def _parse_existing_gitignore_rules(content: str) -> set[str]:
    """解析 .gitignore 已有规则（去重、忽略注释/空行）"""
    rules: set[str] = set()
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        rules.add(s)
    return rules


def _auto_generate_or_append_gitignore(cwd: str) -> dict[str, Any]:
    """根据策略自动生成/追加 .gitignore

    策略：
        1. 项目无 .gitignore → 创建完整版（按 section 分组 + 注释）
        2. 项目已有 .gitignore → 仅追加缺失的规则（保留原内容）
        3. 已处理过的 cwd 不再重复处理（同会话内 set 缓存）

    Returns:
        {"action": "created"|"appended"|"noop",
         "added": int,                # 新增的规则条数
         "sections": list[str]}       # 涉及到的 section 名
    """
    if not cwd:
        return {"action": "noop", "added": 0, "sections": []}
    norm = str(Path(cwd).resolve())
    if norm in _GITIGNORE_UPDATED:
        return {"action": "noop", "added": 0, "sections": []}
    _GITIGNORE_UPDATED.add(norm)

    gitignore = Path(cwd) / ".gitignore"

    try:
        if gitignore.exists():
            existing_text = gitignore.read_text(encoding="utf-8")
            existing_rules = _parse_existing_gitignore_rules(existing_text)
        else:
            existing_text = ""
            existing_rules = set()
    except OSError:
        return {"action": "noop", "added": 0, "sections": []}

    # 计算每个 section 缺失的规则
    missing_by_section: dict[str, list[str]] = {}
    total_missing = 0
    for section, rules in _GITIGNORE_SECTIONS:
        miss = [r for r in rules if r not in existing_rules]
        if miss:
            missing_by_section[section] = miss
            total_missing += len(miss)

    # 情况 1：.gitignore 不存在 → 整文件创建
    if not gitignore.exists():
        lines = [
            "# .gitignore (auto-generated by format_memory_context hook)",
            "# 按需调整即可，已存在的规则会被自动跳过。",
        ]
        for section, rules in _GITIGNORE_SECTIONS:
            lines.append("")
            lines.append(f"# ── {section} ──")
            lines.extend(rules)
        try:
            gitignore.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return {
                "action": "created",
                "added": total_missing,
                "sections": [s for s, _ in _GITIGNORE_SECTIONS],
            }
        except OSError:
            return {"action": "noop", "added": 0, "sections": []}

    # 情况 2：有 .gitignore 但无缺失 → noop
    if total_missing == 0:
        return {"action": "noop", "added": 0, "sections": []}

    # 情况 3：追加缺失的规则（按 section 分组，清晰分隔）
    append_lines: list[str] = []
    append_lines.append("")
    append_lines.append("# ── Auto-appended by format_memory_context hook ──")
    for section, rules in _GITIGNORE_SECTIONS:
        if section not in missing_by_section:
            continue
        append_lines.append("")
        append_lines.append(f"# {section}")
        append_lines.extend(missing_by_section[section])
    append_lines.append("")

    try:
        # 原内容保留，只在末尾追加
        with gitignore.open("a", encoding="utf-8") as f:
            f.write("\n".join(append_lines))
        return {
            "action": "appended",
            "added": total_missing,
            "sections": list(missing_by_section.keys()),
        }
    except OSError:
        return {"action": "noop", "added": 0, "sections": []}


def _build_git_status_block(cwd: str) -> list[str] | None:
    """组装 Git 状态段，返回 None 表示不可用（让调用方 fallback）"""
    if not _auto_git_init(cwd):
        return None
    branch_info = _collect_branch_info(cwd)
    files = _collect_file_status(cwd)
    diff_stats = _collect_diff_stats(cwd)
    stash_count = _collect_stash_count(cwd)
    commits = _collect_recent_commits(cwd)

    parts: list[str] = ["## Git 仓库状态", _format_branch(branch_info)]
    if stash_count:
        parts.append(f"**Stash**: {stash_count} 条未保存的工作")
    parts.extend(_format_status_section(files, diff_stats))

    # 自动处理 .gitignore（无条件跑一次，缓存保证同 cwd 只处理一次）：
    #   - 无 .gitignore → 创建完整版
    #   - 已有 → 仅追加缺失规则
    gi = _auto_generate_or_append_gitignore(cwd)
    if gi["action"] == "created":
        n = gi["added"]
        parts.append("")
        parts.append(
            f"✅ 已自动创建 `.gitignore`（{n} 条规则，覆盖 "
            f"{len(gi['sections'])} 类：{'、'.join(gi['sections'])}）"
        )
    elif gi["action"] == "appended":
        n = gi["added"]
        sections = "、".join(gi["sections"])
        parts.append("")
        parts.append(f"✅ 已自动追加 {n} 条规则到 `.gitignore`（{sections}）")

    # 临时文件提示（Opt 4）
    temp_files = _detect_temp_files(files["untracked"])
    if temp_files:
        names = ", ".join(f"`{p}`" for p in temp_files[:3])
        more = f" 等 {len(temp_files)} 个" if len(temp_files) > 3 else ""
        parts.append("")
        parts.append(f"💡 检测到疑似临时调试文件：{names}{more}（建议清理或加入 .gitignore）")

    parts.append("")
    parts.append(_format_recent_commits(commits))

    result = "\n".join(parts)
    if len(result) > _MAX_CONTEXT_LENGTH:
        result = result[:_MAX_CONTEXT_LENGTH] + "\n\n...(内容过长已截断)"
    return result.split("\n")


# ============================================================
# Hook 入口
# ============================================================


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

        # 优先复用 Git 状态（与 .drifox/plugins/git-status 同款格式）
        git_block = _build_git_status_block(project_root) if project_root else None
        if git_block:
            ctx_lines.append("")
            ctx_lines.extend(git_block)
        elif worktree:
            # fallback：git 不可用时回到 backend 预取的 worktree 字段
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
