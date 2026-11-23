# -*- coding: utf-8 -*-
"""
Git Worktree 检测和管理工具

提供：
- 检测目录是否为 git 仓库
- 列出仓库的所有 worktree
- 判断当前目录是否在 worktree 中
- 获取 worktree 的详细信息
"""

import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import List, Optional

from loguru import logger

# Windows 下隐藏 cmd 窗口
_CREATION_FLAGS = 0
if sys.platform == "win32":
    _CREATION_FLAGS = subprocess.CREATE_NO_WINDOW  # 0x08000000

# 统一编码处理，避免中文乱码崩溃
_SUBPROCESS_KWARGS = {
    "capture_output": True,
    "text": True,
    "encoding": "utf-8",
    "errors": "replace",
    "timeout": 5,
    "creationflags": _CREATION_FLAGS,
}


def _run_git(args: list, cwd: str) -> subprocess.CompletedProcess:
    """执行 git 命令（统一处理编码）"""
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        **_SUBPROCESS_KWARGS,
    )


# ============================================================
# 纯文件 git 探测（PERF）
# ============================================================
# 动机（review 实测）：本机每个 git 子进程固定开销 1.1~1.4s（Windows + Defender），
# 冷启动 git 段 4.3~5.6s、FULL refresh 12.3~16.3s（8+N 个子进程）。而仓库结构
# 其实是**纯文件事实**：`.git` 是目录 → 普通仓库；`.git` 是文件（`gitdir: ...`）
# → linked worktree/submodule；`<gitdir>/commondir` 存在 → 有独立 common dir。
# 纯文件读取实测 25~50ms（3 层向上查找 18.5ms 中位 + 读 1~2 个小文件）。
#
# 四种仓库形态（普通/worktree/submodule/bare）结论与 `git rev-parse` 全部一致。
# 唯一盲区：`GIT_DIR` / `GIT_WORK_TREE` / `GIT_COMMON_DIR` 环境变量会改变 git 的
# 判定但不改变文件布局 —— 检测到即回落子进程（触发概率≈0）。
#
# 开关：`DRIFOX_PURE_FILE_GIT=0` 关闭纯文件路径，行为回到改造前（全部走子进程）。


def _pure_file_enabled() -> bool:
    """纯文件探测是否启用（默认启用；`DRIFOX_PURE_FILE_GIT=0` 关闭）。"""
    return os.environ.get("DRIFOX_PURE_FILE_GIT", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _env_overrides_git_dir() -> bool:
    """环境变量是否覆盖了 git 的目录判定（此时文件布局不再可信）。"""
    return bool(
        os.environ.get("GIT_DIR")
        or os.environ.get("GIT_WORK_TREE")
        or os.environ.get("GIT_COMMON_DIR")
    )


def _read_gitfile(gitfile_path: str) -> Optional[str]:
    """读 `.git` 文件（worktree/submodule 形态），返回 gitdir 绝对路径。

    文件内容形如 `gitdir: <相对或绝对路径>`。非该格式、空值、不可读均返回 None。
    """
    try:
        with open(gitfile_path, "r", encoding="utf-8", errors="replace") as f:
            s = f.read(4096).strip()
    except OSError:
        return None
    if not s.startswith("gitdir:"):
        return None
    d = s[7:].strip()
    if not d:
        return None
    return d if os.path.isabs(d) else os.path.abspath(os.path.join(os.path.dirname(gitfile_path), d))


def _resolve_common_dir(gitdir: str) -> Optional[str]:
    """解析 `<gitdir>/commondir`（linked worktree 才有），返回 common dir 绝对路径。

    无 commondir 时 common == gitdir（普通仓库/worktree 自身即 common）。
    """
    cd = os.path.join(gitdir, "commondir")
    if not os.path.isfile(cd):
        return gitdir
    try:
        with open(cd, "r", encoding="utf-8", errors="replace") as f:
            rel = f.read().strip()
    except OSError:
        return None
    if not rel:
        return gitdir
    return os.path.abspath(os.path.join(gitdir, rel))


def find_repo(path: str) -> Optional[tuple]:
    """纯文件向上查找仓库，返回 `(toplevel, gitdir, common)` 或 None。

    - `toplevel`：工作树根（含 `.git` 的那层目录）
    - `gitdir`  ：`.git` 实际指向的目录（worktree 时是 `<common>/worktrees/<名>`）
    - `common`  ：共享目录（普通仓库与 gitdir 相同；worktree 时是主仓库 `.git`）

    环境变量覆盖 git 目录判定时返回 None（调用方回落子进程，保守正确）。
    """
    if _env_overrides_git_dir():
        return None
    p = os.path.abspath(path)
    if not os.path.isdir(p):
        return None
    while True:
        dot = os.path.join(p, ".git")
        if os.path.isdir(dot):
            return (p, dot, dot)
        if os.path.isfile(dot):
            gd = _read_gitfile(dot)
            if not gd or not os.path.isdir(gd):
                return None
            common = _resolve_common_dir(gd)
            if common is None:
                return None
            return (p, gd, common)
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def _read_head_branch(gitdir: str) -> Optional[str]:
    """读 `<gitdir>/HEAD` 得到分支名；detached 或不可读返回 None。

    - 内容 `ref: refs/heads/<name>` → `<name>`
    - 其它（裸 sha = detached，或格式异常）→ None（交由调用方决定回落值）
    """
    try:
        with open(os.path.join(gitdir, "HEAD"), "r", encoding="utf-8", errors="replace") as f:
            s = f.read(4096).strip()
    except OSError:
        return None
    if not s.startswith("ref:"):
        return None
    ref = s[4:].strip()
    if ref.startswith("refs/heads/"):
        name = ref[len("refs/heads/") :].strip()
        return name or None
    return None



@dataclass
class WorktreeInfo:
    """Worktree 信息"""

    path: str  # 绝对路径
    branch: str  # 分支名（如 refs/heads/main → main）
    is_main: bool  # 是否是主仓库
    is_current: bool  # 是否当前所在 worktree
    is_bare: bool = False  # 是否是 bare 仓库
    behind_main: int = 0  # 落后主仓库的提交数
    ahead_main: int = 0  # 超前主仓库的提交数


@dataclass
class GitRepoInfo:
    """Git 仓库信息"""

    root: str  # git 根目录（含 .git）
    is_worktree: bool  # 当前目录是否在 worktree 中
    worktrees: List[WorktreeInfo] = field(default_factory=list)
    current_branch: str = ""


def _finish_worktree(
    worktrees: List[WorktreeInfo],
    current: dict[str, str],
) -> None:
    """解析 `git worktree list --porcelain` 一个 block，追加到 worktrees 列表"""
    path = current.get("path", "")
    if not path:
        return

    branch_raw = current.get("branch", "")
    if branch_raw:
        # refs/heads/<name> → <name>
        if branch_raw.startswith("refs/heads/"):
            branch = branch_raw[11:]
        else:
            branch = branch_raw
    else:
        # 没有 branch 行 = detached HEAD
        branch = "(detached)"

    is_prunable = "prunable" in current
    is_main = os.path.isdir(os.path.join(path, ".git"))
    # git worktree list 第一行是主仓库
    is_current = len(worktrees) == 0

    worktrees.append(
        WorktreeInfo(
            path=path,
            branch=branch,
            is_main=is_main,
            is_current=is_current,
            is_bare=is_prunable,  # 复用 is_bare 表示可清理状态
        )
    )


class GitWorktreeDetector:
    """Git/Worktree 检测器（带内存缓存，避免重复调 git 拖慢 UI）"""

    _git_available: Optional[bool] = None  # 是否安装了 git
    _detect_cache: dict = {}  # {path: (result, timestamp)}
    _is_worktree_cache: dict = {}  # {path: (bool, timestamp)}
    _branch_cache: dict = {}  # {path: (str, timestamp)}
    _worktrees_cache: dict = {}  # {git_root: (List[WorktreeInfo], timestamp)}
    _info_cache: dict = {}  # {path: (GitRepoInfo, timestamp)}
    _CACHE_TTL = 30.0  # 缓存有效期（秒），避免频繁 git 子进程调用拖慢 UI
    # git root 对固定 workdir 极稳定（git init/重建仓库才变），长 TTL 减少
    # 关窗/保存路径的冷启动子进程（Windows + Defender 下每个 git 进程 100-300ms）
    _DETECT_CACHE_TTL = 300.0
    # [PERF] stale-while-revalidate：后台刷新去重集合与锁（主线程永不跑 git 子进程）
    _refreshing: set = set()
    _refresh_lock = threading.Lock()

    @staticmethod
    def _is_git_available() -> bool:
        """检查系统是否安装了 git（仅检测一次，结果缓存）"""
        if GitWorktreeDetector._git_available is not None:
            return GitWorktreeDetector._git_available
        try:
            subprocess.run(
                ["git", "--version"],
                capture_output=True,
                timeout=3,
                creationflags=_CREATION_FLAGS,
            )
            GitWorktreeDetector._git_available = True
        except (FileNotFoundError, OSError):
            GitWorktreeDetector._git_available = False
            logger.info("[GitWorktree] git 未安装，跳过 git 检测")
        return GitWorktreeDetector._git_available

    @staticmethod
    def _cache_get(cache: dict, key: str, ttl: float = None):
        """从缓存获取（检查 TTL）"""
        import time

        if ttl is None:
            ttl = GitWorktreeDetector._CACHE_TTL
        entry = cache.get(key)
        if entry and time.monotonic() - entry[1] < ttl:
            return entry[0]
        return None

    @staticmethod
    def _cache_set(cache: dict, key: str, value):
        """写入缓存"""
        import time

        cache[key] = (value, time.monotonic())

    @staticmethod
    def detect_git(path: str) -> Optional[str]:
        """检测路径是否在 git 仓库中（带缓存）

        注意：只接受目录路径，文件路径直接跳过（避免以文件为 cwd 执行 git 命令报错）
        """
        if not path or not os.path.isdir(path):
            return None
        cached = GitWorktreeDetector._cache_get(
            GitWorktreeDetector._detect_cache, path, GitWorktreeDetector._DETECT_CACHE_TTL
        )
        if cached is not None:
            # 负结果用空串存（见下方写入侧）：None 无法区分「未缓存」与
            # 「已缓存且结果为 None」，直接 return None 会被调用侧当 miss。
            return cached or None

        # [PERF] 纯文件路径优先：命中则不启动任何 git 子进程（省 ~1.1-1.4s）
        if _pure_file_enabled():
            found = find_repo(path)
            if found is not None:
                val = found[0]
                GitWorktreeDetector._cache_set(GitWorktreeDetector._detect_cache, path, val)
                return val

        if not GitWorktreeDetector._is_git_available():
            return None
        try:
            result = _run_git(["rev-parse", "--show-toplevel"], cwd=path)
            if result.returncode == 0:
                val = result.stdout.strip()
                GitWorktreeDetector._cache_set(GitWorktreeDetector._detect_cache, path, val)
                return val
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.debug(f"[GitWorktree] detect_git failed for {path}: {e}")
        # 负结果存空串而非 None：`_cache_get` 用 `entry` 判存在，但 `None` 值
        # 会被调用侧 `if cached is not None` 当 miss → 非 git 目录每次重跑
        # （实测 845/428/405ms 永不命中，本机 21/38 个 workspace 是非 git）。
        GitWorktreeDetector._cache_set(GitWorktreeDetector._detect_cache, path, "")
        return None

    @staticmethod
    def is_valid_worktree_link(path: str) -> bool:
        """纯文件系统校验 .git 文件指向的 gitdir 是否真实存在（zombie 检测）

        worktree 的 .git 是文件，内容形如 "gitdir: <主仓库>/.git/worktrees/<名>"。
        主仓库 .git 被删除/重建后，遗留的 worktree 目录（git 已 prune）仍带着
        这个 .git 文件，但 git 层面已不认（worktree list 不再列出）。
        不起 git 子进程，可在侧栏重建等高频路径安全调用。
        """
        git_path = os.path.join(path, ".git")
        if not os.path.isfile(git_path):
            return False
        try:
            with open(git_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except Exception:
            # 读取失败：.git 文件存在本身是强信号，维持旧判据按 worktree 处理
            return True
        if content.startswith("gitdir:"):
            gitdir_path = content[7:].strip()
            if gitdir_path and not os.path.isabs(gitdir_path):
                gitdir_path = os.path.join(path, gitdir_path)
            return os.path.isdir(gitdir_path)
        return True

    @staticmethod
    def is_worktree(path: str) -> bool:
        """
        判断路径是否在一个 worktree 中（而非主仓库）

        原理：主仓库的 .git 是文件夹，worktree 的 .git 是文件；
        且 .git 文件指向的 gitdir 必须存在（zombie worktree 不算）

        [PERF-close] 结果缓存：.git 为目录时本方法要起 2 个 git 子进程，
        Windows + Defender 下实测每个 100-300ms（本机实测 370~750ms）；发消息/
        保存会话/关窗保存均高频调用本方法。

        TTL 用 `_DETECT_CACHE_TTL`（300s）而非默认 `_CACHE_TTL`（30s）：
        调用间隔实测 min 32s / p50 53s（`_maybe_flush_partial_reply` 节流 25s），
        30s 的 TTL **比调用间隔还短** → 每次刚好过期 → 每次都起 2 个 git 进程。
        这就是「74 次主线程阻塞中 69 次是 is_worktree」的根因。
        删除/新建 worktree 后由 worktree-manager 的 `_invalidate_info_cache`
        显式清缓存（必须包含 `_is_worktree_cache`，否则会认错 worktree）。
        """
        if not path:
            return False
        cached = GitWorktreeDetector._cache_get(
            GitWorktreeDetector._is_worktree_cache, path, GitWorktreeDetector._DETECT_CACHE_TTL
        )
        if cached is not None:
            return cached

        # [PERF] 纯文件路径优先：gitdir != common 即为 worktree。
        # 这一支替换掉原来「.git 为目录」时的 2 次 rev-parse 子进程
        # （--git-common-dir + --git-dir，各 1.1~1.4s，合计省 2.3~2.8s）。
        # 必须用 normcase + abspath 归一后比较：worktree 的 commondir 内容常是
        # 相对路径（`../.git`），而 gitdir 是绝对路径，直接 `!=` 会把普通仓库
        # 也判成 worktree。
        if _pure_file_enabled():
            found = find_repo(path)
            if found is not None:
                _toplevel, gitdir, common = found
                gd_n = os.path.normcase(os.path.abspath(gitdir))
                cm_n = os.path.normcase(os.path.abspath(common))
                result = gd_n != cm_n
                if not result:
                    # 兼容分支：`.git` 是**文件**但 gitdir 内没有 commondir。
                    # 真实 worktree 一定有 commondir；缺它的只有「伪造/半成品」
                    # 布局（单测构造、手工造的工作树）。这类目录按 git 的
                    # `<common>/worktrees/<名>` 布局特征判 worktree，
                    # 与旧实现（.git 是文件 + gitdir 存在 → True）保持一致。
                    # submodule 的 gitdir 是 `<repo>/.git/modules/<名>`，
                    # 父目录名不是 worktrees，故不会误判。
                    if os.path.basename(os.path.dirname(gd_n)) == "worktrees":
                        result = os.path.isdir(gitdir)
                GitWorktreeDetector._cache_set(
                    GitWorktreeDetector._is_worktree_cache, path, result
                )
                return result

        if not GitWorktreeDetector._is_git_available():
            return False
        result = False
        git_path = os.path.join(path, ".git")
        if os.path.isfile(git_path):
            result = GitWorktreeDetector.is_valid_worktree_link(path)
        elif os.path.isdir(git_path):
            try:
                r1 = _run_git(["rev-parse", "--git-common-dir"], cwd=path)
                git_dir = r1.stdout.strip()
                if r1.returncode == 0 and git_dir:
                    r2 = _run_git(["rev-parse", "--git-dir"], cwd=path)
                    local_git_dir = r2.stdout.strip()
                    result = local_git_dir != git_dir and "worktrees" in git_dir
            except Exception:
                result = False
        GitWorktreeDetector._cache_set(GitWorktreeDetector._is_worktree_cache, path, result)
        return result

    @staticmethod
    def get_current_branch(path: str) -> str:
        """获取当前分支名

        [PERF-close] 结果缓存（TTL 30s）：本方法被 get_repo_info 调用，
        而 get_repo_info 在 PreUserMessage 每条消息前触发；TTL 过期后
        每个 git 子进程在 Windows+Defender 下 100-300ms。分支仅在
        显式 git 操作时变化，30s 陈旧窗口可接受。
        """
        cached = GitWorktreeDetector._cache_get(GitWorktreeDetector._branch_cache, path)
        if cached is not None:
            return cached

        # [PERF] 纯文件路径优先：读 <gitdir>/HEAD（detached 时返回空串，与
        # `git branch --show-current` 的输出语义一致）。
        if _pure_file_enabled():
            found = find_repo(path)
            if found is not None:
                branch = _read_head_branch(found[1]) or ""
                GitWorktreeDetector._cache_set(GitWorktreeDetector._branch_cache, path, branch)
                return branch

        if not GitWorktreeDetector._is_git_available():
            return ""
        result = ""
        try:
            r = _run_git(["branch", "--show-current"], cwd=path)
            if r.returncode == 0:
                result = r.stdout.strip()
        except Exception:
            pass
        GitWorktreeDetector._cache_set(GitWorktreeDetector._branch_cache, path, result)
        return result

    @staticmethod
    def list_worktrees(git_root: str, with_counts: bool = True) -> List[WorktreeInfo]:
        """
        列出 git 仓库的所有 worktree

        使用 --porcelain 格式确保路径含空格时也能正确解析。

        Args:
            git_root: git 仓库根目录
            with_counts: 是否计算 ahead/behind（需要提交图遍历，纯文件不可行）。
                False 时走纯文件快速路径，零 git 子进程；True 时走 git
                `worktree list` + N 个 `rev-list`。

        Returns:
            List[WorktreeInfo]: worktree 列表

        [PERF-close] 结果缓存（TTL 30s）：内部 worktree list + N 个并行
        rev-list（每 worktree 一个子进程），Windows 冷启动 300ms+；调用方
        get_repo_info 在 PreUserMessage 每条消息前触发（hook 上下文）。
        worktree 增删由 worktree-manager 的 _invalidate_info_cache 主动
        失效本缓存，30s 陈旧窗口仅覆盖外部 git 命令行操作。

        [PERF] `with_counts=False` 的纯文件路径：
        - 零子进程（原路径 worktree list + N×rev-list 在本机 FULL refresh 实测
          占 12.3~16.3s 的大部分）
        - counts 恒 0（调用方明确不需要时才用这个模式）
        - `is_current` 语义不变（仍取第一条，见「明确不做」说明）
        """
        if not git_root or not os.path.exists(git_root):
            return []

        # ── 纯文件快速路径（不需要 counts 时）──
        if not with_counts and _pure_file_enabled():
            fast = GitWorktreeDetector._list_worktrees_pure_file(git_root)
            if fast is not None:
                # 独立缓存：不能与 with_counts=True 的结果共用 key，
                # 否则快速版（counts=0）会覆盖完整版（有 counts），
                # UI 面板会缺 ahead/behind。
                GitWorktreeDetector._cache_set(
                    GitWorktreeDetector._worktrees_cache, ("fast", git_root), fast
                )
                return fast

        cache_key = ("full", git_root) if with_counts else git_root
        cached = GitWorktreeDetector._cache_get(
            GitWorktreeDetector._worktrees_cache, cache_key
        )
        if cached is not None:
            return cached

        try:
            result = _run_git(["worktree", "list", "--porcelain"], cwd=git_root)
            if result.returncode != 0:
                return []

            # --porcelain 格式（每条 worktree 为一个 block，空行分隔）：
            #   worktree <path>
            #   HEAD <sha1>
            #   branch refs/heads/<name>     # 有分支时
            #   (无 branch 行 = detached HEAD)
            #   prunable gitdir file ...     # 可选，可清理状态
            worktrees: List[WorktreeInfo] = []
            current: dict[str, str] = {}

            for line in result.stdout.splitlines():
                if not line.strip():
                    # 空行 = 一个 worktree block 结束
                    if current:
                        _finish_worktree(worktrees, current)
                        current = {}
                    continue

                if line.startswith("worktree "):
                    current["path"] = line[9:]
                elif line.startswith("HEAD "):
                    current["head"] = line[5:]
                elif line.startswith("branch "):
                    current["branch"] = line[7:]  # refs/heads/<name>
                elif line.startswith("prunable "):
                    current["prunable"] = line[9:]
                # 其他行（如 detached）忽略

            # 最后一段可能没有尾随空行
            if current:
                _finish_worktree(worktrees, current)

            # 计算每个 worktree 相对主仓库的落后/超前提交数。
            # [P3] 并行执行 rev-list（只读命令、无锁冲突）：大仓库 + N 个 worktree
            # 时总耗时从串行 N×T 降为 ≈T，避免 list_worktrees 拖慢调用方
            # （配合 UI 层把本方法移入后台线程，主线程零阻塞）。
            main_branch = None
            for wt in worktrees:
                if wt.is_main:
                    main_branch = wt.branch
                    break
            if main_branch and main_branch != "(detached)":
                targets = [wt for wt in worktrees if not wt.is_main and wt.branch != "(detached)"]
                if targets:
                    import concurrent.futures

                    def _fetch_count(wt: WorktreeInfo):
                        try:
                            rev_result = _run_git(
                                ["rev-list", "--left-right", "--count", f"{main_branch}...{wt.branch}"],
                                cwd=git_root,
                            )
                            if rev_result.returncode == 0:
                                parts = rev_result.stdout.strip().split("\t")
                                if len(parts) == 2:
                                    # left=main_branch, right=worktree_branch
                                    # behind = main 有而 worktree 没有
                                    # ahead  = worktree 有而 main 没有
                                    return wt, int(parts[0]), int(parts[1])
                        except Exception:
                            pass
                        return wt, 0, 0

                    # 并发上限 4：既消除串行累积，又不至于打满子进程
                    workers = min(4, len(targets))
                    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                        for wt, behind, ahead in ex.map(_fetch_count, targets):
                            wt.behind_main = behind
                            wt.ahead_main = ahead

            GitWorktreeDetector._cache_set(
                GitWorktreeDetector._worktrees_cache, cache_key, worktrees
            )
            return worktrees

        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.debug(f"[GitWorktree] list_worktrees failed: {e}")
            return []

    @staticmethod
    def get_repo_info(path: str, with_counts: bool = True) -> Optional[GitRepoInfo]:
        """获取路径的完整 git 仓库信息

        Args:
            with_counts: 是否算 ahead/behind。False 时 list_worktrees 走纯文件
                快速路径（零子进程，counts 恒 0），缓存键也分开存，避免与
                完整版互相覆盖。
        """
        if not path:
            return None

        # [PERF] 纯文件路径不依赖 git 可执行文件，缓存命中/命中纯文件都可先走；
        # git 可用性只在确实需要回落子进程时懒检测（省一次 `git --version`）。
        cache_key = (path, bool(with_counts))
        cached = GitWorktreeDetector._cache_get(GitWorktreeDetector._info_cache, cache_key)
        if cached is not None:
            return cached

        # 标记是否已写入过 None 哨兵：_cache_get 对 None 值与「未缓存」不可区分，
        # 故沿用原行为，直接重算（有 detect_cache 兵底，不会反复起子进程）。
        git_root = GitWorktreeDetector.detect_git(path)
        if not git_root:
            if GitWorktreeDetector._is_git_available():
                GitWorktreeDetector._cache_set(
                    GitWorktreeDetector._info_cache, cache_key, None
                )
            return None

        is_wt = GitWorktreeDetector.is_worktree(path) or GitWorktreeDetector.is_worktree(git_root)
        branch = GitWorktreeDetector.get_current_branch(path)
        worktrees = GitWorktreeDetector.list_worktrees(git_root, with_counts=with_counts)

        info = GitRepoInfo(
            root=git_root,
            is_worktree=is_wt,
            worktrees=worktrees,
            current_branch=branch,
        )
        GitWorktreeDetector._cache_set(GitWorktreeDetector._info_cache, cache_key, info)
        return info

    @staticmethod
    def get_repo_info_stale_while_revalidate(
        path: str, with_counts: bool = True
    ) -> Optional["GitRepoInfo"]:
        """主线程安全版 get_repo_info：命中缓存（忽略 TTL）立即返回，过期后台刷新。

        背景：get_repo_info 缓存过期时会同步跑 5+N 个 git 子进程（detect_git /
        is_worktree ×2 / branch / worktree list + N 个 rev-list，Windows+Defender
        下每个 100-300ms），而 PreUserMessage 每条消息在主线程触发一次上下文构建
        （backend._build_worktree_context_dict）——间隔超 TTL 的首条消息必然卡 UI。

        本方法主线程永不执行 git 子进程：
        - 缓存有任何值（含过期）→ 立即返回旧值；过期则起 daemon 线程刷新
        - 无缓存 → 返回 None，同时起后台线程预热（下次调用命中）

        30s 陈旧窗口对「分支名 / 工作树列表」这类展示型上下文可接受
        （SessionStart 路径 _build_session_context 的 _cache_get 同款语义）。
        缓存失效仍是主动的：worktree-manager 增删工作树时 _invalidate_info_cache。
        """
        if not path:
            return None
        cache_key = (path, bool(with_counts))
        entry = GitWorktreeDetector._info_cache.get(cache_key)
        info = entry[0] if entry else None
        if info is None:
            # 无缓存（首次访问或历史缓存为 None 哨兵）：后台预热，本次拿不到就给 None
            GitWorktreeDetector._spawn_info_refresh(cache_key)
            return None
        import time as _time

        if _time.monotonic() - entry[1] >= GitWorktreeDetector._CACHE_TTL:
            GitWorktreeDetector._spawn_info_refresh(cache_key)
        return info

    @staticmethod
    def _spawn_info_refresh(cache_key) -> None:
        """后台线程刷新 repo info（按 (path, with_counts) 去重：同键在跑则跳过）。

        去重键必须是完整缓存键而非单一 path：快速版（with_counts=False）与
        完整版（True）结果不同，共用去重键会让其中一种的刷新被误挡掉。
        """
        if isinstance(cache_key, tuple):
            path, with_counts = cache_key
        else:  # 兼容旧调用（只传 path）
            path, with_counts = cache_key, True
        with GitWorktreeDetector._refresh_lock:
            if cache_key in GitWorktreeDetector._refreshing:
                return
            GitWorktreeDetector._refreshing.add(cache_key)

        def _run():
            try:
                GitWorktreeDetector.get_repo_info(path, with_counts=with_counts)
            except Exception:
                pass
            finally:
                with GitWorktreeDetector._refresh_lock:
                    GitWorktreeDetector._refreshing.discard(cache_key)

        threading.Thread(target=_run, daemon=True, name="git-info-refresh").start()

    @staticmethod
    def _list_worktrees_pure_file(git_root: str) -> Optional[List[WorktreeInfo]]:
        """纯文件枚举 worktree（zero subprocess）。无法判定时返回 None 回落 git。

        数据来源：
        - 主工作树：`git_root` 自身（`.git` 存在即主仓库/普通仓库）
        - 子工作树：`<common>/worktrees/<name>/gitdir` 文件指向该工作树根
        - 分支：读各 gitdir 的 `HEAD`
        - prunable：`<name>/gitdir` 指向的目录不存在（对应 `--porcelain` 的 prunable）

        `is_main` / `is_current` 口径与 `_finish_worktree` 完全一致：
        - `is_main` = `<path>/.git` 是**目录**（主仓库形态）
        - `is_current` = 列表第一条（保持现状语义，见模块说明「明确不做」）
        counts 恒 0（纯文件读不出提交图）。
        """
        found = find_repo(git_root)
        if found is None:
            return None
        toplevel, _gitdir, common = found
        if not os.path.isdir(common):
            return None

        out: List[WorktreeInfo] = []
        # 1) 主工作树（列表第一条 → is_current=True，与 _finish_worktree 同口径）
        main_branch = _read_head_branch(common) or "(detached)"
        out.append(
            WorktreeInfo(
                path=toplevel,
                branch=main_branch,
                is_main=os.path.isdir(os.path.join(toplevel, ".git")),
                is_current=True,
                is_bare=False,
            )
        )

        # 2) 子工作树：<common>/worktrees/*/gitdir
        wt_base = os.path.join(common, "worktrees")
        if not os.path.isdir(wt_base):
            return out
        try:
            names = sorted(os.listdir(wt_base))
        except OSError:
            return None
        for name in names:
            entry_dir = os.path.join(wt_base, name)
            if not os.path.isdir(entry_dir):
                continue
            gitdir_file = os.path.join(entry_dir, "gitdir")
            wt_path = None
            if os.path.isfile(gitdir_file):
                try:
                    with open(gitdir_file, "r", encoding="utf-8", errors="replace") as f:
                        wt_path = f.read(4096).strip() or None
                except OSError:
                    wt_path = None
            if not wt_path:
                continue
            # gitdir 文件指向 `<工作树根>/.git`，其父目录才是工作树根
            if os.path.basename(wt_path) == ".git":
                wt_root = os.path.dirname(wt_path)
            else:
                wt_root = wt_path
            branch = _read_head_branch(entry_dir) or "(detached)"
            prunable = not os.path.isdir(wt_root)
            out.append(
                WorktreeInfo(
                    path=wt_root,
                    branch=branch,
                    is_main=False,
                    is_current=False,
                    is_bare=prunable,  # 复用 is_bare 表示可清理（同 _finish_worktree）
                )
            )
        return out

    @staticmethod
    def get_main_repo_path(worktree_path: str) -> Optional[str]:
        """从 worktree 路径反查主仓库根目录

        Worktree 的 .git 是一个文件，内容格式为：
            gitdir: /path/to/main/.git/worktrees/name
        （Windows: gitdir: D:/path/to/main/.git/worktrees/name）
        通过向上三层即可得到主仓库根目录。
        """
        git_file = os.path.join(worktree_path, ".git")
        if not os.path.isfile(git_file):
            return None
        try:
            with open(git_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content.startswith("gitdir:"):
                gitdir_path = content[7:].strip()
                # .git/worktrees/name → .git/worktrees → .git → repo root
                return os.path.dirname(os.path.dirname(os.path.dirname(gitdir_path)))
        except Exception:
            pass
        return None

    @staticmethod
    def main_repo_from_worktree_path(path: str) -> Optional[str]:
        """从**已删除**的 worktree 路径推断主仓库根（纯字符串运算，不碰文件系统）

        工作树目录被删后 ``.git`` 文件也不在了，``get_main_repo_path`` 失效。
        但路径形态固定为 ``<主仓库>/.worktrees/<名>``（本程序 manage_worktree
        创建时即如此，见 plugins/worktree-manager/tools/worktree_tools.py），
        据此反推主仓库根。主仓库本身（不在 .worktrees 下）返回 None。
        """
        if not path:
            return None
        norm = str(path).replace("\\", "/").rstrip("/")
        marker = "/.worktrees/"
        idx = norm.find(marker)
        if idx <= 0:
            return None
        # 只认一层：<主仓库>/.worktrees/<名>，多一层说明不是本程序创建的
        if "/" in norm[idx + len(marker):]:
            return None
        return norm[:idx]

    @staticmethod
    def resolve_workdir_or_main(path: str) -> Optional[str]:
        """工作目录失效时回落到主仓库根

        - 目录存在 → 原样返回（不判 git，非 git 目录也照常可用）
        - 目录不存在且是 ``<主仓库>/.worktrees/<名>`` → 返回主仓库根
        - 其余（从未存在/主仓库也找不到）→ None，调用方按「未设置」处理
        """
        if not path:
            return None
        raw = str(path)
        if os.path.isdir(raw):
            return raw
        main_root = GitWorktreeDetector.main_repo_from_worktree_path(raw)
        if main_root and os.path.isdir(main_root):
            logger.info(f"[GitWorktree] 工作目录已失效，回落到主仓库: {raw} → {main_root}")
            return main_root
        return None


