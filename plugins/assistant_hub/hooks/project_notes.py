# -*- coding: utf-8 -*-
"""project_notes.py — assistant_hub hooks（项目笔记 + 项目上下文）

两个 hook（替代原 system 插件的 read_project_notes / format_memory_context:hook_git）：

1. ``hook_notes``（BuildSystemPrompt）：读取 {workdir}/AGENTS.md 项目笔记注入
   （不存在时用默认模板创建）。
2. ``hook_context``（PreUserMessage）：注入项目根目录路径规则 + git 仓库状态
   （自动 git init / .gitignore 生成等副作用逻辑自原 system hook 原样迁移）。

注入开关（助手级配置，主智能体生效）：
- ``Assistant.project_notes_enabled``   → hook_notes
- ``Assistant.project_context_enabled`` → hook_context
子智能体（current_role=subagent）始终注入；主智能体无激活助手时不注入。

实现说明：
- HookWorker 经 spec_from_file_location 独立加载本文件（无 package 上下文），
  禁止相对导入；assistant_manager 模块按路径加载并缓存 sys.modules
  （模块名与 ui/hooks 侧一致 → AssistantManager 单例共享）。
"""

from __future__ import annotations

import concurrent.futures
import importlib.util
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict

from loguru import logger

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_MANAGER_MODULE_NAME = "assistant_hub_manager"

# Windows 专属：防止 subprocess 调 git 时弹出黑色 cmd 窗口
_CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# ============================================================
# AssistantManager 加载（与 inject_assistant.py 同款约定）
# ============================================================


def _get_manager():
    """按路径加载 assistant_manager.py（sys.modules 单例，与 ui/hooks 共享）。"""
    mod = sys.modules.get(_MANAGER_MODULE_NAME)
    if mod is not None:
        return mod.AssistantManager.get_instance()
    source = _PLUGIN_ROOT / "assistant_manager.py"
    try:
        mtime = source.stat().st_mtime
    except OSError:
        mtime = 0.0
    spec = importlib.util.spec_from_file_location(_MANAGER_MODULE_NAME, str(source))
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MANAGER_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
        module._source_mtime = mtime
    except Exception as e:
        logger.error(f"[assistant_hub.project_notes] 加载 assistant_manager 失败: {e}")
        return None
    return module.AssistantManager.get_instance()


def _enabled_for_primary(context: Dict[str, Any], attr: str) -> bool:
    """判断当前会话是否允许注入。

    - 子智能体（current_role != "primary"）→ 始终允许
    - 主智能体 → 读当前激活助手的开关字段；无激活助手 → 不允许
    """
    ctx = context or {}
    if ctx.get("current_role") != "primary":
        return True
    try:
        mgr = _get_manager()
        if mgr is None:
            return False
        aid = mgr.active_id()
        if not aid or not mgr.has(aid):
            return False
        a = mgr.get(aid)
        return bool(getattr(a, attr, True))
    except Exception as e:
        logger.warning(f"[assistant_hub.project_notes] 读取助手开关失败: {e}")
        return False


# ============================================================
# 项目笔记 hook（原 system 插件 read_project_notes.py 迁移）
# ============================================================

INITIAL_TEMPLATE = """# 项目开发规范

本文件为 AI Agent 提供项目操作手册与约束清单，确保 Agent 行为可控、可复现。

---

## 1. 目标与边界

### 允许的操作
- **有关键文档存在时，优先以关键文档作为项目路径进行探索**
- 读取、修改顶层文档：`README.md`、`AGENTS.md`、`CONTRIBUTING.md` 等
- 读取、修改 `docs/`、`prompts/`、`skills/`、`tools/config/`、`tools/external/` 下的文档与代码
- 执行项目规定的 lint、检查、构建命令
- 新增/修改功能、修复问题
- 提交符合规范的 commit

### 禁止的操作
- 修改 `.github/workflows/` 中的 CI 配置（除非任务明确要求）
- 修改 `LICENSE`、`CODE_OF_CONDUCT.md`
- 在代码中硬编码密钥、Token 或敏感凭证
- 未经确认的大范围重构

### 敏感区域（禁止自动修改）
- `.github/workflows/*.yml` - CI/CD 配置
- `.env*` 文件（如存在）

---

## 2. 推荐执行路径

```bash
# 1. 拉取最新代码
git pull --rebase origin develop

# 2. 初始化依赖（如有需要）
# ... 项目特有命令

# 3. 运行 lint 检查
# ... 项目特有命令

# 4. 执行修改任务
# ...

# 5. 再次验证
# ... 项目特有检查命令

# 6. 提交变更
git add -A
git commit -m "feat|fix|docs|chore: scope - summary"
git push origin develop
```

---

## 3. 修改约束

### 架构原则
- 保持根目录扁平，避免巨石文件
- 遵循项目现有架构，不随意改动

### 禁止行为
- 禁止"顺手重构/大范围改动"除非任务明确要求
- 禁止删除现有测试用例（除非任务要求）
- 禁止在代码中硬编码敏感信息

---

## 4. 风格与质量标准

### 格式化工具
- 遵循项目现有代码风格
- 使用项目已有的格式化工具

### 命名约定
- 文档、注释、日志使用中文
- 代码符号统一英文且语义直白
- 文件名小写加中划线或下划线（遵循现有风格）

### 设计品味
- 优先消除分支与重复
- 函数单一职责且短小

---

## 5. 提交规范

遵循简化 Conventional Commits：
```
feat|fix|docs|chore|refactor|test: scope - summary
```

---

## 6. 强制同步规则

**任何功能/命令/配置/目录/工作流变化必须同步更新相关文档**

不确定的内容用 TODO 标注，不允许猜测。
"""


def hook_notes(event: str, context: dict) -> str:
    """BuildSystemPrompt hook：注入项目笔记（AGENTS.md），按助手开关控制"""
    if not _enabled_for_primary(context, "project_notes_enabled"):
        return ""

    workdir = (context or {}).get("project_root", "")
    project_name = (context or {}).get("project_name", "")

    if not workdir:
        # compaction/gateway 等无真实项目上下文的情况，跳过注入
        logger.debug(f"[assistant_hub.project_notes] 无项目工作目录，跳过项目笔记: project_name={project_name}")
        return ""

    agents_path = Path(workdir) / "AGENTS.md"
    notes_content = ""

    if agents_path.exists():
        try:
            notes_content = agents_path.read_text(encoding="utf-8")
            # 兜底修复：文件存在但内容为空/纯空白时，按"未初始化"处理
            if not notes_content.strip():
                logger.warning(f"[assistant_hub.project_notes] AGENTS.md 存在但内容为空，重新初始化: {agents_path}")
                agents_path.write_text(INITIAL_TEMPLATE, encoding="utf-8")
                notes_content = INITIAL_TEMPLATE
        except Exception as e:
            logger.error(f"[assistant_hub.project_notes] 读取 {agents_path} 失败: {e}")
            notes_content = ""
    else:
        # 文件不存在 → 用默认模板创建（新建项目首次运行此路径）
        try:
            agents_path.parent.mkdir(parents=True, exist_ok=True)
            agents_path.write_text(INITIAL_TEMPLATE, encoding="utf-8")
            logger.info(f"[assistant_hub.project_notes] 已创建项目笔记文件: {agents_path}")
            notes_content = INITIAL_TEMPLATE
        except Exception as e:
            logger.error(f"[assistant_hub.project_notes] 创建 {agents_path} 失败: {e}")

    if notes_content:
        return f"## 项目笔记\n[当前项目: {project_name}]\n{notes_content}"
    return f"## 项目笔记\n[当前项目: {project_name}]\n（项目笔记为空）"


# ============================================================
# Git 状态采集（自 system 插件 format_memory_context.py 原样迁移）
# ============================================================

_GIT_TIMEOUT = 1.5
_MAX_CONTEXT_LENGTH = 2000
_MAX_STAGED_ITEMS = 30
_MAX_UNSTAGED_ITEMS = 30
_MAX_UNTRACKED_ITEMS = 20
_MAX_RECENT_COMMITS = 5
# 同一 cwd 只尝试一次自动 git init，避免重复触发破坏性操作
_AUTO_INITED: set[str] = set()

# 同 cwd 在 TTL 秒内复用 git 命令结果，避免反复跑 git
_GIT_CACHE_TTL = 5.0
_GIT_CACHE: dict[str, tuple[float, Any]] = {}
# git 是否安装：一个进程只检查一次
_GIT_AVAILABLE: bool | None = None


def _run_git(cwd: str, *args: str) -> tuple[str, str, int]:
    """执行 git 命令并返回 (stdout, stderr, returncode)，异常转为 returncode=-1"""
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
        return result.stdout.rstrip("\n"), result.stderr.rstrip("\n"), result.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", -1
    except FileNotFoundError:
        return "", "git not found", -1
    except Exception:
        return "", "error", -1


def _is_git_available() -> bool:
    """检查系统是否安装了 git（进程级缓存，只实际执行一次 git --version）"""
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
    """处理 PyInstaller 打包后的路径问题（_internal 临时目录 → 真实项目目录）"""
    resolved = str(Path(cwd).resolve())

    if "_internal" not in resolved:
        return resolved

    internal_dir = Path(resolved).parent
    parent_of_internal = internal_dir.parent
    project_name = Path(resolved).name

    potential_src = parent_of_internal / project_name
    if potential_src.exists() and potential_src.is_dir():
        logger.info(f"[assistant_hub.project_notes] PyInstaller 检测：使用源码目录 {potential_src}")
        return str(potential_src)

    if (parent_of_internal / ".git").exists():
        logger.info(f"[assistant_hub.project_notes] PyInstaller 检测：使用父目录 {parent_of_internal}")
        return str(parent_of_internal)

    return resolved


def _auto_git_init(cwd: str) -> bool:
    """若项目不是 git 仓库但系统装了 git，则自动 git init + 空 commit"""
    if not cwd:
        return False

    resolved = str(Path(cwd).resolve())
    resolved = _resolve_pyinstaller_path(resolved)

    # 安全检查：避免在根目录 / 家目录 / PyInstaller 临时目录中 init
    resolved_path = Path(resolved)
    dangerous_parents = {Path("/"), Path.home(), Path(sys.executable).parent if getattr(sys, "frozen", False) else None}
    dangerous_parents.discard(None)
    if resolved_path in dangerous_parents or "_internal" in resolved:
        logger.warning(f"[assistant_hub.project_notes] 安全检查：拒绝在危险位置 git init: {resolved}")
        return False

    if _is_git_repo(resolved):
        return True
    if not _is_git_available():
        return False
    if resolved in _AUTO_INITED:
        return _is_git_repo(resolved)
    _AUTO_INITED.add(resolved)

    logger.info(f"[assistant_hub.project_notes] 自动 git init: {resolved}")
    _, _, code = _run_git(resolved, "init")
    if code != 0:
        return False
    _, stderr, code = _run_git(resolved, "commit", "--allow-empty", "-m", "init")
    if code != 0:
        logger.warning(f"[assistant_hub.project_notes] 空 commit 失败: {stderr}")
        return False
    return _is_git_repo(resolved)


def _parse_branch_header(line: str) -> dict[str, Any]:
    """解析 `git status --porcelain=v1 --branch` 输出里的 ## 头行"""
    out: dict[str, Any] = {"branch": "", "ahead": 0, "behind": 0, "is_detached": False}
    if line.startswith("## HEAD (detached at "):
        m = re.search(r"detached at ([0-9a-f]+)", line)
        if m:
            out["branch"] = f"(detached @ {m.group(1)})"
            out["is_detached"] = True
        return out

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
    """解析 git status --porcelain=v1 --branch 的输出"""
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
    """并发跑所有 git 命令并合并结果。结果按 cwd 缓存 5 秒。"""
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
    """格式化文件列表，可选附加每文件 diff 行数"""
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


# 疑似临时调试文件命名模式
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
# 同一 cwd 只处理一次 .gitignore
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


def _auto_generate_gitignore(cwd: str) -> dict[str, Any]:
    """按需自动生成 .gitignore（已有则不动，同 cwd 只处理一次）"""
    if not cwd:
        return {"action": "noop", "added": 0, "sections": []}
    norm = str(Path(cwd).resolve())
    if norm in _GITIGNORE_UPDATED:
        return {"action": "noop", "added": 0, "sections": []}
    _GITIGNORE_UPDATED.add(norm)

    gitignore = Path(cwd) / ".gitignore"

    if gitignore.exists():
        return {"action": "noop", "added": 0, "sections": []}

    lines = [
        "# .gitignore (auto-generated by assistant_hub project_notes hook)",
        "# 按需调整即可。",
    ]
    total = 0
    for section, rules in _GITIGNORE_SECTIONS:
        lines.append("")
        lines.append(f"# ── {section} ──")
        lines.extend(rules)
        total += len(rules)
    try:
        gitignore.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {
            "action": "created",
            "added": total,
            "sections": [s for s, _ in _GITIGNORE_SECTIONS],
        }
    except OSError:
        return {"action": "noop", "added": 0, "sections": []}


def _build_git_status_block(cwd: str) -> list[str] | None:
    """组装 Git 状态段，返回 None 表示不可用"""
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

    gi = _auto_generate_gitignore(resolved)
    if gi["action"] == "created":
        n = gi["added"]
        parts.append("")
        parts.append(
            f"✅ 已自动创建 `.gitignore`（{n} 条规则，覆盖 {len(gi['sections'])} 类：{'、'.join(gi['sections'])}）"
        )

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
# 项目上下文 hook（原 format_memory_context:hook_git 迁移，保持 PreUserMessage）
# ============================================================


def hook_context(event: str, context: dict) -> str:
    """PreUserMessage hook：注入项目根目录路径规则 + git 仓库状态，按助手开关控制"""
    if not _enabled_for_primary(context, "project_context_enabled"):
        return ""

    project_root = (context or {}).get("project_root", "")
    if not project_root:
        return ""

    ctx_lines = ["## 项目上下文"]
    ctx_lines.append(f"- 项目根目录: {project_root}")
    ctx_lines.append("- 根目录内：用相对路径（如 `src/main.py`），节省 token")
    ctx_lines.append("- 根目录外：用绝对路径")

    git_block = _build_git_status_block(project_root)
    if git_block:
        ctx_lines.append("")
        ctx_lines.extend(git_block)

    return "\n".join(ctx_lines)
