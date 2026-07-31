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

import concurrent.futures
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from loguru import logger

# Windows 专属：防止 subprocess 调 git 时弹出黑色 cmd 窗口
# CREATE_NO_WINDOW = 0x08000000，仅 Windows 有效
_CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# ============================================================
# Git 状态收集（移植自 .drifox/plugins/git-status/hooks/git_status.py）
# ============================================================

_GIT_TIMEOUT = 1.5
_MAX_CONTEXT_LENGTH = 2000
_MAX_STAGED_ITEMS = 30
_MAX_UNSTAGED_ITEMS = 30
_MAX_UNTRACKED_ITEMS = 20
_MAX_RECENT_COMMITS = 5
# 同一 cwd 只尝试一次自动 git init，避免每条用户消息都触发破坏性操作
_AUTO_INITED: set[str] = set()

# === 性能优化（方案 A + B）===
# 同 cwd 在 TTL 秒内复用 git 命令结果，避免每轮 PreUserMessage 反复跑 8 次 git
_GIT_CACHE_TTL = 5.0
_GIT_CACHE: dict[str, tuple[float, Any]] = {}
# git 是否安装：一个进程只检查一次
_GIT_AVAILABLE: bool | None = None


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
            creationflags=_CREATE_NO_WINDOW,
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
    """检查系统是否安装了 git（cwd='.' 不依赖具体目录）

    进程级缓存：只在第一次调用时实际执行 git --version
    """
    global _GIT_AVAILABLE
    if _GIT_AVAILABLE is None:
        _, _, code = _run_git(".", "--version")
        _GIT_AVAILABLE = code == 0
    return _GIT_AVAILABLE


def _is_git_repo(cwd: str) -> bool:
    """检查目录是否在 Git 仓库中（含子目录）"""
    if not cwd:
        return False
    path = Path(cwd)
    if not path.exists() or not path.is_dir():
        return False
    _, _, code = _run_git(cwd, "rev-parse", "--is-inside-work-tree")
    return code == 0


def _resolve_pyinstaller_path(cwd: str) -> str:
    """处理 PyInstaller 打包后的路径问题

    PyInstaller 打包后运行在 _internal 临时目录中，此时：
    - cwd 可能是 _internal/.drifox/workspaces/xxx/，而非真实项目目录
    - 真实的 git 仓库在 exe 同级目录或用户指定的源码目录

    返回可用的真实项目路径。如果找不到，返回原始 cwd。
    """
    resolved = str(Path(cwd).resolve())

    # 如果不在 _internal 目录下，直接返回
    if "_internal" not in resolved:
        return resolved

    # 在 _internal 下，尝试向上查找可能的真实项目目录
    # 常见模式：xxx/_internal/.drifox/workspaces/项目名
    #           xxx/_internal/项目名/（单项目打包）
    #           xxx/_internal/（直接运行在 _internal 内）

    # 尝试向上查找真实项目目录
    internal_dir = Path(resolved).parent  # 向上到 _internal
    parent_of_internal = internal_dir.parent
    project_name = Path(resolved).name

    # 检查 _internal 同级是否有同名目录（源码）
    potential_src = parent_of_internal / project_name
    if potential_src.exists() and potential_src.is_dir():
        logger.info(f"[format_memory_context] PyInstaller 检测：使用源码目录 {potential_src}")
        return str(potential_src)

    # 检查 _internal 同级是否有 .git
    if (parent_of_internal / ".git").exists():
        logger.info(f"[format_memory_context] PyInstaller 检测：使用父目录 {parent_of_internal}")
        return str(parent_of_internal)

    # 返回 resolved，让后续逻辑自然失败并 fallback
    return resolved


def _auto_git_init(cwd: str) -> bool:
    """若项目不是 git 仓库但系统装了 git，则自动 git init + 空 commit

    用于让 model 在非 git 项目里也能感知到默认分支名（init 后立刻有 main/master）。

    Returns:
        True  → 项目已经是 git 仓库（无论是否本次 init）
        False → 没有 git 或 init 失败
    """
    if not cwd:
        return False

    resolved = str(Path(cwd).resolve())

    # PyInstaller 打包后处理：尝试找到真实源码目录
    resolved = _resolve_pyinstaller_path(resolved)

    # 安全检查：避免在根目录 / 家目录 / PyInstaller 临时目录中 init
    resolved_path = Path(resolved)
    dangerous_parents = {Path("/"), Path.home(), Path(sys.executable).parent if getattr(sys, "frozen", False) else None}
    dangerous_parents.discard(None)
    if resolved_path in dangerous_parents or "_internal" in resolved:
        logger.warning(f"[format_memory_context] 安全检查：拒绝在危险位置 git init: {resolved}")
        return False

    if _is_git_repo(resolved):
        return True
    if not _is_git_available():
        return False
    # 同一目录只 init 一次，避免每轮用户消息都触发
    if resolved in _AUTO_INITED:
        return _is_git_repo(resolved)
    _AUTO_INITED.add(resolved)

    logger.info(f"[format_memory_context] 自动 git init: {resolved}")
    _, _, code = _run_git(resolved, "init")
    if code != 0:
        return False
    # 空 commit 让默认分支立即产生（否则 branch --show-current 返回空）
    _, stderr, code = _run_git(resolved, "commit", "--allow-empty", "-m", "init")
    if code != 0:
        logger.warning(f"[format_memory_context] 空 commit 失败: {stderr}")
        return False
    return _is_git_repo(resolved)


def _parse_branch_header(line: str) -> dict[str, Any]:
    """解析 `git status --porcelain=v1 --branch` 输出里的 ## 头行

    支持：
        ## dev                              → branch=dev, ahead=0, behind=0
        ## dev...origin/dev                 → branch=dev
        ## dev...origin/dev [ahead 2, behind 0]
        ## HEAD (detached at abc123)        → branch="(detached @ abc123)"
        ## master (root-commit) ...         → branch=master
    """
    out: dict[str, Any] = {"branch": "", "ahead": 0, "behind": 0, "is_detached": False}
    if line.startswith("## HEAD (detached at "):
        m = re.search(r"detached at ([0-9a-f]+)", line)
        if m:
            out["branch"] = f"(detached @ {m.group(1)})"
            out["is_detached"] = True
        return out

    # 标准格式：## branch[...upstream] [ahead N, behind N]
    # branch 用 greedy [^\s.]+：从首字符开始匹配，遇到空白或点停止
    m = re.match(
        r"^## (?P<branch>[^\s.]+)(?:\.{3}(?P<up>[^\s\[]+))?"
        r"(?: \[ahead (?P<ahead>\d+)(?:, behind (?P<behind>\d+))?\])?",
        line,
    )
    if m:
        out["branch"] = m.group("branch")
        if m.group("ahead"):
            out["ahead"] = int(m.group("ahead"))
        if m.group("behind"):
            out["behind"] = int(m.group("behind"))
    return out


def _parse_status_v1(out: str) -> dict[str, Any]:
    """解析 git status --porcelain=v1 --branch 的输出

    头行 ## ... 携带分支信息；其余 XY path 行携带文件状态。
    """
    files: dict[str, list] = {"staged": [], "unstaged": [], "untracked": []}
    branch_info: dict[str, Any] = {"branch": "", "ahead": 0, "behind": 0, "is_detached": False}

    for line in out.splitlines():
        if not line:
            continue
        if line.startswith("## "):
            branch_info = _parse_branch_header(line)
            continue
        if len(line) < 3:
            continue
        x = line[0]
        y = line[1]
        path = line[3:]
        if x == "?" and y == "?":
            files["untracked"].append(path)
        else:
            if x != " ":
                files["staged"].append((x, path))
            if y != " ":
                files["unstaged"].append((y, path))
    return {"branch_info": branch_info, "files": files}


def _parse_numstat(out: str) -> dict[str, tuple[int, int]]:
    """解析 git diff --numstat 输出（按 tab 分：added<TAB>removed<TAB>path）"""
    stats: dict[str, tuple[int, int]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if added == "-" and removed == "-":
            stats[path] = (0, 0)  # 二进制
        else:
            try:
                stats[path] = (int(added), int(removed))
            except ValueError:
                continue
    return stats


def _collect_all_git(cwd: str) -> dict[str, Any]:
    """并发跑所有 git 命令并合并结果。结果按 cwd 缓存 5 秒。

    命令合并：原本 8 个串行命令 → 4 个并行命令：
        1. git status --porcelain=v1 --branch   （分支 + ahead/behind + 文件状态）
        2. git diff --numstat                    （每个文件 diff 行数）
        3. git log -n5 --pretty=format:%h %s (%cr) （最近 commits）
        4. git stash list                        （stash 数）

    Returns:
        {
            "branch": str, "ahead": int, "behind": int, "is_detached": bool,
            "files": {"staged": [...], "unstaged": [...], "untracked": [...]},
            "diff_stats": {path: (added, removed)},
            "stash_count": int,
            "commits": list[str],
        }
    """
    # 5 秒内同 cwd 复用
    now = time.monotonic()
    cached = _GIT_CACHE.get(cwd)
    if cached is not None:
        ts, val = cached
        if now - ts < _GIT_CACHE_TTL:
            return val

    empty: dict[str, Any] = {
        "branch": "",
        "ahead": 0,
        "behind": 0,
        "is_detached": False,
        "files": {"staged": [], "unstaged": [], "untracked": []},
        "diff_stats": {},
        "stash_count": 0,
        "commits": [],
    }

    def _branch_and_status() -> str:
        out, _, code = _run_git(
            cwd,
            "-c",
            "core.quotepath=false",
            "status",
            "--porcelain=v1",
            "--branch",
            "-uall",
            "--no-renames",
        )
        return out if code == 0 else ""

    def _diff() -> str:
        out, _, code = _run_git(cwd, "-c", "core.quotepath=false", "diff", "--numstat")
        return out if code == 0 else ""

    def _commits() -> str:
        out, _, code = _run_git(
            cwd,
            "log",
            f"-n{_MAX_RECENT_COMMITS}",
            "--pretty=format:%h %s (%cr)",
        )
        return out if code == 0 else ""

    def _stash() -> str:
        out, _, code = _run_git(cwd, "stash", "list")
        return out if code == 0 else ""

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            f_bs = ex.submit(_branch_and_status)
            f_diff = ex.submit(_diff)
            f_commits = ex.submit(_commits)
            f_stash = ex.submit(_stash)
            bs_out = f_bs.result()
            diff_out = f_diff.result()
            commits_out = f_commits.result()
            stash_out = f_stash.result()
    except Exception:
        return empty

    if not bs_out:
        return empty

    parsed = _parse_status_v1(bs_out)
    branch_info = parsed["branch_info"]
    files = parsed["files"]

    result = {
        "branch": branch_info["branch"],
        "ahead": branch_info["ahead"],
        "behind": branch_info["behind"],
        "is_detached": branch_info["is_detached"],
        "files": files,
        "diff_stats": _parse_numstat(diff_out),
        "stash_count": len(stash_out.splitlines()) if stash_out else 0,
        "commits": commits_out.splitlines() if commits_out else [],
    }
    _GIT_CACHE[cwd] = (now, result)
    return result


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
    ),
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
    """组装 Git 状态段，返回 None 表示不可用（让调用方 fallback）

    一次性从 _collect_all_git 拿到所有数据（已并发 + 已缓存），
    本函数只负责格式化，不再做任何 subprocess 调用。
    """
    # 统一使用 resolved 绝对路径，确保与 _auto_git_init 内部一致
    resolved = str(Path(cwd).resolve())
    if not _auto_git_init(resolved):
        return None
    info = _collect_all_git(resolved)
    branch = info["branch"]
    ahead = info["ahead"]
    behind = info["behind"]
    files = info["files"]
    diff_stats = info["diff_stats"]
    stash_count = info["stash_count"]
    commits = info["commits"]

    parts: list[str] = [
        "## Git 仓库状态",
        f"**当前分支**: `{branch}`" + (f" ↑{ahead}" if ahead else "") + (f" ↓{behind}" if behind else ""),
    ]
    if stash_count:
        parts.append(f"**Stash**: {stash_count} 条未保存的工作")
    parts.extend(_format_status_section(files, diff_stats))

    # 自动处理 .gitignore（无条件跑一次，缓存保证同 resolved 只处理一次）：
    #   - 无 .gitignore → 创建完整版
    #   - 已有 → 仅追加缺失规则
    gi = _auto_generate_or_append_gitignore(resolved)
    if gi["action"] == "created":
        n = gi["added"]
        parts.append("")
        parts.append(
            f"✅ 已自动创建 `.gitignore`（{n} 条规则，覆盖 {len(gi['sections'])} 类：{'、'.join(gi['sections'])}）"
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
    """注入条目记忆 + 关键文档（长期记忆）

    Args:
        event: 事件名称（PreUserMessage）
        context: 由 backend 预取的上下文，含：
            - entry_memories: list[str] 条目记忆列表
            - key_documents: list[dict] 关键文档列表

    Returns:
        格式化的长期记忆字符串
    """
    entry_memories = context.get("entry_memories", [])
    key_documents = context.get("key_documents", [])

    mem_lines = ["### 条目记忆"]
    if entry_memories:
        for content in entry_memories:
            mem_lines.append(f"- {content}")
    else:
        mem_lines.append("- 暂无条目记忆")
    mem_lines.append("")

    mem_lines.append("### 关键文档")
    if key_documents:
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
    else:
        mem_lines.append("- 暂无关键文档")

    if not entry_memories and not key_documents:
        return ""
    return "\n".join(mem_lines)


def hook_git(event: str, context: dict) -> str:
    """注入项目上下文 + Git 仓库状态

    Args:
        event: 事件名称（PreUserMessage）
        context: 由 backend 预取的上下文，含：
            - project_root: str 当前窗口工作目录
            - project_name: str 当前项目名
            - worktree: dict (可选) repo_name/current_branch/workdir/is_worktree/other_branches

    Returns:
        格式化的项目上下文字符串（含 Git 状态）
    """
    project_root = context.get("project_root", "")
    worktree = context.get("worktree")

    if not project_root:
        return ""

    ctx_lines = []
    ctx_lines.append(f"- 项目根目录: {project_root}")
    ctx_lines.append("- 根目录内：用相对路径（如 `src/main.py`），节省 token")
    ctx_lines.append("- 根目录外：用绝对路径")

    git_block = _build_git_status_block(project_root) if project_root else None
    if git_block:
        ctx_lines.append("")
        ctx_lines.extend(git_block)
    elif worktree:
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

    return "\n".join(ctx_lines)
