# -*- coding: utf-8 -*-
"""project_notes.py — assistant_hub hooks（项目笔记 + 项目上下文）

三个 hook（替代原 system 插件的 read_project_notes / format_memory_context:hook_git）：

1. ``hook_notes``（BuildSystemPrompt）：读取 {workdir}/AGENTS.md 项目笔记注入
   （不存在时用默认模板创建）。
2. ``hook_context``（PreUserMessage）：注入项目根目录路径规则 + git 仓库状态
   （自动 git init / .gitignore 生成等副作用逻辑自原 system hook 原样迁移）。
   主智能体同会话内上下文实质未变时不重复注入（按内容指纹去重，相对时间
   漂移不算变化）；子智能体始终注入。
3. ``reset_context_dedup``（SessionStart clear/compact）：清空去重指纹，让
   历史被清空/摘要后能重新注入。

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
import hashlib
import importlib.util
import os
import re
import subprocess
import sys
import threading
import time
import types
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
    - 主智能体 → 读当前生效助手的开关字段（会话级临时助手 override 优先，
      回落主助手）；无生效助手 → 不允许
    """
    ctx = context or {}
    if ctx.get("current_role") != "primary":
        return True
    try:
        mgr = _get_manager()
        if mgr is None:
            return False
        sid = str(ctx.get("session_id") or "")
        aid = mgr.get_session_override(sid) if sid else ""
        if not aid:
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
        return f"<project_notes>\n[当前项目: {project_name}]\n{notes_content}\n</project_notes>"
    return f"<project_notes>\n[当前项目: {project_name}]\n（项目笔记为空）\n</project_notes>"


# ============================================================
# Git 状态采集（自 system 插件 format_memory_context.py 原样迁移）
# ============================================================

_GIT_TIMEOUT = 5.0
_MAX_CONTEXT_LENGTH = 2000
_MAX_STAGED_ITEMS = 30
_MAX_UNSTAGED_ITEMS = 30
_MAX_UNTRACKED_ITEMS = 20
_MAX_RECENT_COMMITS = 5
# 同一 cwd 只尝试一次自动 git init，避免重复触发破坏性操作
_AUTO_INITED: set[str] = set()

# ── 工作树签名短路（PERF）────────────────────────────────────────────
# 背景实测（2026-11-23，本机 .git 有 22119 个文件）：
#   git --version                 = 1030ms   ← 这台机器每个 git 进程固定开销
#   git status --porcelain -uall  = 4683ms
#   4 条并发墙钟                  = 4951ms   ← 每次用户发送都付一次
# 原来只有 5s TTL，用户两条消息间隔 >5s 就是每次都白跑 5 秒。
#
# 修法：在真正跑 git 之前先取「工作树签名」（4 次 stat ≈ 0.1ms），签名与上次
# 一致则完全跳过 git 子进程，直接复用上次结果。
#
# 签名必须能覆盖「已追踪文件内容被改」这类不改 index/目录 mtime 的变化 ——
# 单靠签名会漏，故叠加两条兜底：
#   1) 签名**变化或缺失**时一律真跑 git（保守，不漏）
#   2) 即使签名相同，超过 `_GIT_REFRESH_TTL` 也强制刷新一次
#      → 语义从「每次都跑」降为「每 N 秒最多跑一次」，允许 ≤N 秒陈旧
# 这样纯内容修改最迟 N 秒后一定反映；`git add`/新建文件走签名变化路径立即反映。
_GIT_REFRESH_TTL = 30.0
# HEAD 侧兜底 TTL：新提交/切分支的 reflog / refs 信号很可靠，故 TTL 可以长一些；
# 即便信号漏了（如 packed-refs 被外部改写但 mtime 未变），5 分钟内也会强制刷一次。
_GIT_HEAD_REFRESH_TTL = 300.0
# 采集失败（超时等）后的退避：本机 `git status -uall` 实测 4.7s、超时上限 5s。
# 退避必须 ≥ 一次超时的墙钟，否则等于没有退避、每一轮都要白等 5s；
# 又要远小于两侧 TTL（30s / 300s），保证一个 TTL 周期内有多次重试机会，
# 不会把一次瞬时空闲放大成 5 分钟陈旧。
_GIT_FAIL_BACKOFF = 5.0
# 签名缓存按 cwd 键（实践中个位数），设上限防插件被指到大量临时目录时无界增长
_GIT_PEN_MAX = 32
# git 目录向上探测最大层数（常规仓库 cwd 即根，1 层命中）
_GIT_DIR_MAX_WALK = 8
# cwd -> (ts_work, ts_head, work_sig, head_sig, result_dict)
# ts_* 是**该侧最后一次成功采集**的时刻，两侧独立判 TTL（失败侧会被回拨）
_GIT_SIG_CACHE: dict[str, tuple[float, float, Any, Any, dict]] = {}

# git 是否安装：一个进程只检查一次
_GIT_AVAILABLE: bool | None = None


def _resolve_git_dir(cwd: str) -> Path | None:
    """从 cwd 向上找 `.git`，返回**该工作树专属** git 目录（不追到共享目录）。

    - 常规仓库：`<root>/.git` 目录
    - linked worktree / submodule：`.git` 是文件，内容 `gitdir: <path>`，
      指向 `.git/worktrees/<name>` 之类的专属 gitdir
      （HEAD、index、logs/HEAD 都在这里，不在主仓库 `.git`）

    零子进程（只 stat + 读两个小文件）：签名计算路径上绝不能起 git 进程，
    本机每个 git 进程固定开销约 0.7~1s。找不到返回 None。
    """
    try:
        cur = Path(cwd)
    except (OSError, ValueError):
        return None
    for _ in range(_GIT_DIR_MAX_WALK):
        dot_git = cur / ".git"
        try:
            if dot_git.is_dir():
                return dot_git
            if dot_git.is_file():
                text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
                if text.startswith("gitdir:"):
                    target = Path(text[len("gitdir:") :].strip())
                    if not target.is_absolute():
                        target = dot_git.parent / target
                    target = Path(os.path.normpath(target))
                    return target if target.is_dir() else None
                return None
        except OSError:
            return None
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent
    return None


def _git_common_dir(git_dir: Path) -> Path:
    """git_dir 对应的**共享**目录：linked worktree 的 gitdir 内有 `commondir`
    文件（内容形如 `../..`），refs 与 packed-refs 在共享目录里。
    无 commondir（常规仓库）时即返回自身。"""
    try:
        marker = git_dir / "commondir"
        if marker.is_file():
            text = marker.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                target = Path(text)
                if not target.is_absolute():
                    target = git_dir / target
                target = Path(os.path.normpath(target))
                if target.is_dir():
                    return target
    except OSError:
        pass
    return git_dir


def _git_work_sig(cwd: str) -> Any:
    """**工作树信号**：文件增删 / staged 变化。

    跟踪项：
    - `.git/index` mtime_ns → git add / rm / checkout（暂存区变化）
    - 工作树根目录 mtime_ns → 新建/删除**顶层**文件

    已知盲区（由 status 组的 `_GIT_REFRESH_TTL` 兜底）：只改已追踪文件**内容**
    不改任何 mtime；在**子目录**内新建文件也不改根目录 mtime。
    """
    try:
        git_dir = _resolve_git_dir(cwd)
        if git_dir is None:
            return None
        return (
            (git_dir / "index").stat().st_mtime_ns,
            Path(cwd).stat().st_mtime_ns,
        )
    except OSError:
        return None


def _git_head_sig(cwd: str) -> Any:
    """**HEAD 信号**：新提交 / 切分支 / reset / rebase / fetch。

    跟踪项（全部是 stat，不逐个文件遍历）：
    - `<git_dir>/logs/HEAD` mtime_ns → **主信号**。reflog 是追加写单文件，
      commit / reset --soft / rebase / fetch / checkout 只要移动 ref 就写它。
      不能依赖 `refs/heads` 目录 mtime：重写**已有** ref 文件内容（新提交、
      `reset --soft HEAD~1` 移动分支 ref）不改父目录 mtime，只有增删文件才改。
    - `<git_dir>/HEAD` mtime_ns → 切分支 / detach
    - refs 目录本身及**一级子目录** mtime_ns → 新建分支/标签
    - `packed-refs` mtime_ns（存在时）→ git gc / 大量分支操作
      refs / packed-refs 同时看共享目录（linked worktree 下它们在 commondir）。

    降级：`core.logAllRefUpdates=false` 时 reflog 不写、logs/HEAD 不存在，
    此时只剩 HEAD + 目录 mtime 项（与改造前同量级），签名仍会变，不会退化成常量。

    ⚠️ **不能用 `os.walk` 逐文件 stat**：本仓 `.git/refs` 只有 49 个文件，
    但 Windows 下逐个 `stat` 实测 **204.8ms**（每次都是独立系统调用），
    这会把「无变化时应为 0ms」的验收拖成 256ms。目录 mtime 在子项增删时
    由文件系统自动更新；「重写已有 ref 内容」这类变化由 logs/HEAD 覆盖。
    """
    git_dir = _resolve_git_dir(cwd)
    if git_dir is None:
        return None
    try:
        parts: list[int] = [(git_dir / "HEAD").stat().st_mtime_ns]
    except OSError:
        return None
    # reflog：所有 ref 移动都会追加写它，这是 reset --soft 唯一可见的信号
    try:
        parts.append((git_dir / "logs" / "HEAD").stat().st_mtime_ns)
    except OSError:
        pass  # 未开 reflog（core.logAllRefUpdates=false）：退回下面的目录 mtime 兜底
    seen: set[str] = set()
    for base in (git_dir, _git_common_dir(git_dir)):
        key = os.path.normpath(str(base))
        if key in seen:
            continue
        seen.add(key)
        refs_dir = Path(base) / "refs"
        if refs_dir.is_dir():
            parts.append(refs_dir.stat().st_mtime_ns)
            # 只取一级子目录（heads / tags / remotes / ...），不递归文件
            try:
                with os.scandir(refs_dir) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                parts.append(entry.stat(follow_symlinks=False).st_mtime_ns)
                        except OSError:
                            continue
            except OSError:
                pass
        packed = Path(base) / "packed-refs"
        if packed.exists():
            try:
                parts.append(packed.stat().st_mtime_ns)
            except OSError:
                pass
    return tuple(parts)


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

    # 空仓库（无任何提交）：## No commits yet on <branch>
    m0 = re.match(r"^## No commits yet on (?P<branch>\S+)", line)
    if m0:
        out["branch"] = m0.group("branch")
        return out

    if line.startswith("## HEAD (detached at "):
        m = re.search(r"detached at ([0-9a-f]+)", line)
        if m:
            out["branch"] = f"(detached @ {m.group(1)})"
            out["is_detached"] = True
        return out

    # 分支名懒惰匹配：不吞 upstream（dev...origin/dev 中 branch=dev）；
    # 不排除点号（release/1.2.3 完整保留）；ahead/behind 裸词独立解析，
    # 兼容 [ahead N] / [behind N] / [ahead N, behind N] 三种形态
    m = re.match(r"^## (?P<branch>[^\s\[]+?)(?:\.{3}(?P<up>[^\s\[]+))?(?:\s|$)", line)
    if m:
        out["branch"] = m.group("branch")
        if ma := re.search(r"\bahead (\d+)", line):
            out["ahead"] = int(ma.group(1))
        if mb := re.search(r"\bbehind (\d+)", line):
            out["behind"] = int(mb.group(1))
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


def _head_ref_unborn(cwd: str) -> bool:
    """HEAD 指向的分支 ref 尚无对象（空仓库 / 未曾提交）→ True。

    只在 `git log` 返回非 0 时调用（罕见路径），零子进程。用来区分
    「空仓库的 legit 128」与「超时 / 其他错误的失败」：前者 commits 应清空成「(无)」，
    后者必须保留上一轮值。
    """
    git_dir = _resolve_git_dir(cwd)
    if git_dir is None:
        return False
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return False
    if not head.startswith("ref:"):
        return False  # detached / 裸 sha：一定指向已有提交
    ref = head[4:].strip()
    for base in (git_dir, _git_common_dir(git_dir)):
        try:
            if (Path(base) / ref).exists():
                return False
            # linked worktree：refs 在共享目录，packed 后也可能没有 loose ref 文件
            packed = Path(base) / "packed-refs"
            if packed.is_file() and ref in packed.read_text(
                encoding="utf-8", errors="replace"
            ):
                return False
        except OSError:
            continue
    return True


def _git_sig_store(
    cwd: str,
    ts_work: float,
    ts_head: float,
    work_sig: Any,
    head_sig: Any,
    result: dict,
) -> None:
    """写双信号缓存（带简单上限：超限丢最早插入的一条，避免无界增长）。

    `ts_work` / `ts_head` 是**各自最后一次成功采集**的时刻；失败侧被 `_backoff_ts`
    回拨，两侧独立判 TTL，互不影响（共享一个 ts 会把另一侧成功侧的 TTL 一起推迟）。
    """
    if len(_GIT_SIG_CACHE) >= _GIT_PEN_MAX and cwd not in _GIT_SIG_CACHE:
        try:
            _GIT_SIG_CACHE.pop(next(iter(_GIT_SIG_CACHE)))
        except (StopIteration, KeyError):
            pass
    _GIT_SIG_CACHE[cwd] = (ts_work, ts_head, work_sig, head_sig, result)


def _backoff_ts(now: float, ttl: float) -> float:
    """失败侧的假时间戳：使该侧在 `_GIT_FAIL_BACKOFF` 秒内不重跑，
    之后立刻允许重试（而不是等满整个 ttl）。

    为什么不用「完全不动缓存（传 prev_ts）」：失败时缓存里的签名已经换成新值，
    若 ts 不动，need_* 恒为 True → 每一轮都重跑那条烧满 5s 的超时进程，
    用户每次发送都要白等。回拨 ts 让退避期内的相同签名复用旧结果。
    """
    return now - max(0.0, ttl - _GIT_FAIL_BACKOFF)


def _collect_all_git(cwd: str) -> dict[str, Any]:
    """并发跑所有 git 命令并合并结果。

    [PERF] 两个**独立**信号，两组结果**分别缓存、互不覆盖**：

    | 命令组 | 触发条件 | 兜底 TTL |
    |---|---|---|
    | status(+`-uall`) + diff（两路并发） | `work_sig` 变 | 30s |
    | log -n5 --oneline（单条） | `head_sig` 变 | 300s |

    `work_sig` 未变时仍复用上次 status/diff 结果；`head_sig` 未变时仍复用上次
    log 结果。任一组触发时只跑该组的命令——不是「两个 TTL 共用一次全量跑」。

    采集量（2026-11-23 本机实测，3 次中位，本仓 / 临时空仓 ms）：
    - `git --version`                 641 / 740   ← 单进程固定开销 ~700ms
    - `status --branch -uall`        2393 / 906
    - `diff --numstat`               1965 / 784
    - `log -n5`（含 %cr）             1550 / 1012
    - `stash list`                   1773 / 734

    保留 `-uall`（untracked 到文件级）与 `log`（最近提交）。
    去掉 `stash list`：信息价值低，且 stash 变化会同时改 refs（被 head_sig 覆盖），
    需要时重加即可。
    保留 `--branch`（ahead/behind 必需）与 `--no-renames`（现有解析器把
    `old -> new` 整体当路径，认不了 `R` 行）。

    ⚠️ 原 `_GIT_CACHE_TTL`（5s）次级复用**已移除**：它会绕过签名判定直接返回
    旧结果，导致「签名都变了却还是旧数据」。

    空变化时直接返回**缓存本体**（同一 dict 引用），调用方只读、禁止原地修改。
    """
    now = time.monotonic()

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

    # ── 两个信号（各 ~2 次 stat / 一次 refs walk）──
    # 非 git 目录返回 None；用稳定哨兵参与比对，否则「非 git 目录」永远命中
    # 不了缓存，每次发送都白跑一次 git（实测 1.4s）。
    raw_work = _git_work_sig(cwd)
    work_sig = raw_work if raw_work is not None else ("__no_git__", cwd)
    raw_head = _git_head_sig(cwd)
    head_sig = raw_head if raw_head is not None else ("__no_git__", cwd)

    cached = _GIT_SIG_CACHE.get(cwd)
    prev_work_sig = prev_head_sig = None
    prev_result: dict[str, Any] = empty
    prev_ts_work = prev_ts_head = 0.0
    if cached is not None:
        prev_ts_work, prev_ts_head, prev_work_sig, prev_head_sig, prev_result = cached

    need_work = not (
        prev_work_sig == work_sig and (now - prev_ts_work) < _GIT_REFRESH_TTL
    )
    need_head = not (
        prev_head_sig == head_sig and (now - prev_ts_head) < _GIT_HEAD_REFRESH_TTL
    )

    # 两组都不需要 → 0 个子进程，直接返回上次结果（缓存本体，调用方只读）
    if not need_work and not need_head:
        return prev_result

    def _store_failure(exc: Exception | None = None) -> dict[str, Any]:
        """整组采集异常：结果沿用上次，**失败侧**回拨 ts 做短退避（见 `_backoff_ts`）。"""
        if exc is not None:
            logger.debug(f"[project_notes] git 采集异常（{cwd}）: {exc!r}")
        _git_sig_store(
            cwd,
            _backoff_ts(now, _GIT_REFRESH_TTL) if need_work else prev_ts_work,
            _backoff_ts(now, _GIT_HEAD_REFRESH_TTL) if need_head else prev_ts_head,
            work_sig,
            head_sig,
            prev_result,
        )
        return prev_result

    # ── 准备要跑的命令（只跑需要的那组）──
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

    def _log() -> tuple[str, int]:
        """`git log` → (输出, code)。空仓库识别为合法空态返回 ("", 0)。"""
        out, err, code = _run_git(cwd, "log", f"-n{_MAX_RECENT_COMMITS}", "--oneline")
        if code == 0:
            return out, 0
        if code == 128 and _head_ref_unborn(cwd):
            # 空仓库（无任何提交）git log 返回 128，这是 legit 空态，界面显示「(无)」
            logger.debug(f"[project_notes] 空仓库无提交（{cwd}）: {err}")
            return "", 0
        logger.debug(f"[project_notes] git log 失败（{cwd}）code={code}: {err}")
        return "", code

    bs_out: str | None = None
    diff_out: str | None = None
    log_out: str | None = None  # None = log 真失败；"" = 空仓库（合法空态）

    if need_work and need_head:
        # 三路并发：log(1.4s) < status(2.4s)，不额外增墙钟
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
                f_bs = ex.submit(_branch_and_status)
                f_diff = ex.submit(_diff)
                f_log = ex.submit(_log)
                bs_out, diff_out = f_bs.result(), f_diff.result()
                log_text, log_code = f_log.result()
                log_out = log_text if log_code == 0 else None
        except Exception as exc:
            return _store_failure(exc)
    elif need_work:
        # 只文件侧变化：只跑 status + diff
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                f_bs = ex.submit(_branch_and_status)
                f_diff = ex.submit(_diff)
                bs_out, diff_out = f_bs.result(), f_diff.result()
        except Exception as exc:
            return _store_failure(exc)
    else:
        # 只 HEAD 侧变化：只跑 log 一条
        try:
            log_text, log_code = _log()
            log_out = log_text if log_code == 0 else None
        except Exception as exc:
            return _store_failure(exc)

    # ── 合并：**逐组**判成败，成功组用新结果、失败组沿用上次并退避 ──
    # 两组必须分开：status 超时不能让已经成功拿到的 log 结果一起丢掉（否则 commits
    # 被压制到 300s 后才刷）。
    result: dict[str, Any] = dict(prev_result)  # 浅拷贝，保留未更新的组
    ts_work = prev_ts_work
    ts_head = prev_ts_head

    if need_work:
        if bs_out:
            parsed = _parse_status_v1(bs_out)
            branch_info = parsed["branch_info"]
            result.update(
                {
                    "branch": branch_info["branch"],
                    "ahead": branch_info["ahead"],
                    "behind": branch_info["behind"],
                    "is_detached": branch_info["is_detached"],
                    "files": parsed["files"],
                    "diff_stats": _parse_numstat(diff_out or ""),
                }
            )
            ts_work = now
        else:
            # status 无输出：非仓库、git 失败或**超时**。保留上次结果，work 侧退避
            logger.debug(f"[project_notes] git status 无输出（{cwd}），沿用上次结果并退避")
            ts_work = _backoff_ts(now, _GIT_REFRESH_TTL)

    if need_head:
        if log_out is not None:
            result["commits"] = log_out.splitlines() if log_out else []
            ts_head = now
        else:
            # log 真失败（超时 / git 缺失 / 其他错误）：**不得**把 commits 写成空，
            # 否则界面把「最近 commits」显示成 (无) 并缓存 300s
            ts_head = _backoff_ts(now, _GIT_HEAD_REFRESH_TTL)

    # stash 已不采集：字段恒 0，保持下游取键结构不变
    result.setdefault("stash_count", 0)

    _git_sig_store(cwd, ts_work, ts_head, work_sig, head_sig, result)
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


# ---- 注入去重 ----
# 同一会话内上下文实质未变时跳过重复注入：每轮都注入会在历史里堆出多份
# 相同的「## 项目上下文」消息（长会话 token 浪费的主因之一）。
# 状态按 (session_id, project_root) 记指纹，仅进程内有效；进程重启或模块
# 热重载后重新注入一次，代价可忽略。
_CONTEXT_FP_MAX = 256

# 状态挂 sys.modules 固定键（见 _dedup_state 注释），不能用模块级 dict
_DEDUP_STATE_KEY = "_drifox_project_notes_dedup_state"

def _dedup_state():
    """取跨模块实例共享的指纹表（FP / LOCK）

    HookWorker 以「函数路径」为键缓存函数对象，同一文件的 hook_context 与
    reset_context_dedup 会被各自 exec 成独立模块，模块级 dict 互不可见
    （clear/compact 清的是另一份空表，等于没清）。
    sys.modules.setdefault 保证并发首建只留一份。
    """
    state = sys.modules.get(_DEDUP_STATE_KEY)
    if state is not None:
        return state
    created = types.ModuleType(_DEDUP_STATE_KEY)
    created.FP = {}
    created.LOCK = threading.Lock()
    return sys.modules.setdefault(_DEDUP_STATE_KEY, created)

# git 相对时间（`(4 minutes ago)` / `(4 分钟前)`）随真实时间漂移，不属实质变化，
# 计算指纹前抹平，否则每分钟都会判定「变了」
_REL_TIME_RE = re.compile(r"\(\s*\d[^()]*?(?:ago|前)\s*\)")

def _context_fingerprint(text: str) -> str:
    """上下文指纹：抹平相对时间漂移后取 sha1"""
    normalized = _REL_TIME_RE.sub("(…)", text)
    return hashlib.sha1(normalized.encode("utf-8", "replace")).hexdigest()

def _should_inject(key: str, text: str) -> bool:
    """与上次注入内容比对：相同返回 False（跳过），不同记录并返回 True"""
    fp = _context_fingerprint(text)
    state = _dedup_state()
    with state.LOCK:
        if state.FP.get(key) == fp:
            return False
        state.FP[key] = fp
        overflow = len(state.FP) - _CONTEXT_FP_MAX
        if overflow > 0:
            for stale in list(state.FP)[:overflow]:
                state.FP.pop(stale, None)
    return True

def reset_context_dedup(event: str, context: dict) -> str:
    """SessionStart(clear/compact) hook：清空注入指纹

    clear 后历史被清空、compact 后旧注入可能已被摘要掉，两种情况都必须允许
    下一轮重新注入，否则模型会丢失项目上下文。
    """
    state = _dedup_state()
    with state.LOCK:
        state.FP.clear()
    return ""

def hook_context(event: str, context: dict) -> str:
    """PreUserMessage hook：注入项目根目录路径规则 + git 仓库状态，按助手开关控制

    去重只针对主智能体（同会话内上下文未变则跳过）；子智能体每次都拿到全新
    上下文，始终注入。
    """
    ctx = context or {}
    if not _enabled_for_primary(ctx, "project_context_enabled"):
        return ""

    project_root = ctx.get("project_root", "")
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

    text = "\n".join(ctx_lines)

    if ctx.get("current_role") == "primary":
        key = f"{ctx.get('session_id') or ''}\x00{project_root}"
        if not _should_inject(key, text):
            logger.debug("[assistant_hub.project_notes] 项目上下文未变化，跳过重复注入")
            return ""
    return text
