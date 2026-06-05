# -*- coding: utf-8 -*-
"""
Git Worktree 检测和管理工具

提供：
- 检测目录是否为 git 仓库
- 列出仓库的所有 worktree
- 判断当前目录是否在 worktree 中
- 获取 worktree 的详细信息
"""

import subprocess
import os
import sys
import functools
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


@dataclass
class WorktreeInfo:
    """Worktree 信息"""
    path: str          # 绝对路径
    branch: str        # 分支名（如 refs/heads/main → main）
    is_main: bool      # 是否是主仓库
    is_current: bool   # 是否当前所在 worktree
    is_bare: bool = False  # 是否是 bare 仓库
    behind_main: int = 0   # 落后主仓库的提交数
    ahead_main: int = 0    # 超前主仓库的提交数


@dataclass
class GitRepoInfo:
    """Git 仓库信息"""
    root: str                # git 根目录（含 .git）
    is_worktree: bool        # 当前目录是否在 worktree 中
    worktrees: List[WorktreeInfo] = field(default_factory=list)
    current_branch: str = ""


class GitWorktreeDetector:
    """Git/Worktree 检测器（带内存缓存，避免重复调 git 拖慢 UI）"""

    _git_available: Optional[bool] = None  # 是否安装了 git
    _detect_cache: dict = {}               # {path: (result, timestamp)}
    _info_cache: dict = {}                 # {path: (GitRepoInfo, timestamp)}
    _CACHE_TTL = 30.0                      # 缓存有效期（秒），避免频繁 git 子进程调用拖慢 UI

    @staticmethod
    def _is_git_available() -> bool:
        """检查系统是否安装了 git（仅检测一次，结果缓存）"""
        if GitWorktreeDetector._git_available is not None:
            return GitWorktreeDetector._git_available
        try:
            subprocess.run(
                ["git", "--version"],
                capture_output=True, timeout=3,
                creationflags=_CREATION_FLAGS,
            )
            GitWorktreeDetector._git_available = True
        except (FileNotFoundError, OSError):
            GitWorktreeDetector._git_available = False
            logger.info("[GitWorktree] git 未安装，跳过 git 检测")
        return GitWorktreeDetector._git_available

    @staticmethod
    def _cache_get(cache: dict, key: str):
        """从缓存获取（检查 TTL）"""
        import time
        entry = cache.get(key)
        if entry and time.monotonic() - entry[1] < GitWorktreeDetector._CACHE_TTL:
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
        if not GitWorktreeDetector._is_git_available():
            return None
        cached = GitWorktreeDetector._cache_get(GitWorktreeDetector._detect_cache, path)
        if cached is not None:
            return cached
        try:
            result = _run_git(["rev-parse", "--show-toplevel"], cwd=path)
            if result.returncode == 0:
                val = result.stdout.strip()
                GitWorktreeDetector._cache_set(GitWorktreeDetector._detect_cache, path, val)
                return val
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.debug(f"[GitWorktree] detect_git failed for {path}: {e}")
        GitWorktreeDetector._cache_set(GitWorktreeDetector._detect_cache, path, None)
        return None

    @staticmethod
    def is_worktree(path: str) -> bool:
        """
        判断路径是否在一个 worktree 中（而非主仓库）
        
        原理：主仓库的 .git 是文件夹，worktree 的 .git 是文件
        """
        if not path:
            return False
        git_path = os.path.join(path, ".git")
        if os.path.isfile(git_path):
            return True
        elif os.path.isdir(git_path):
            try:
                result = _run_git(["rev-parse", "--git-common-dir"], cwd=path)
                git_dir = result.stdout.strip()
                if result.returncode != 0 or not git_dir:
                    return False
                result2 = _run_git(["rev-parse", "--git-dir"], cwd=path)
                local_git_dir = result2.stdout.strip()
                return local_git_dir != git_dir and "worktrees" in git_dir
            except Exception:
                pass
            return False
        return False

    @staticmethod
    def get_current_branch(path: str) -> str:
        """获取当前分支名"""
        try:
            result = _run_git(["branch", "--show-current"], cwd=path)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def list_worktrees(git_root: str) -> List[WorktreeInfo]:
        """
        列出 git 仓库的所有 worktree
        
        Args:
            git_root: git 仓库根目录
        
        Returns:
            List[WorktreeInfo]: worktree 列表
        """
        if not git_root or not os.path.exists(git_root):
            return []
        
        try:
            result = _run_git(["worktree", "list"], cwd=git_root)
            if result.returncode != 0:
                return []
            
            import re
            # 格式: /path/to/dir  abc1234 [branch-name]  optional-status
            # 例: D:/work/DriFoxx  96be02b [dev]
            # 例: D:/work/DriFoxx-wt  96be02b [feature/test] prunable
            line_re = re.compile(r'^(\S+)\s+(\S+)\s+\[([^\]]+)\](.*)$')
            
            worktrees = []
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                
                m = line_re.match(line)
                if not m:
                    continue
                
                path = m.group(1)
                # commit_hash = m.group(2)  # 不需要
                branch_raw = m.group(3)
                status = m.group(4).strip()
                
                # 解析分支名
                # [dev] → dev
                # [refs/heads/dev] → dev  
                # [(detached HEAD)] → detached
                if branch_raw.startswith("refs/heads/"):
                    branch = branch_raw[11:]
                elif "detached" in branch_raw.lower():
                    branch = "(detached)"
                else:
                    branch = branch_raw
                
                is_prunable = "prunable" in status
                is_main = os.path.isdir(os.path.join(path, ".git"))
                is_current = len(worktrees) == 0
                
                worktrees.append(WorktreeInfo(
                    path=path,
                    branch=branch,
                    is_main=is_main,
                    is_current=is_current,
                    is_bare=is_prunable,  # 复用 is_bare 表示可清理状态
                ))
            
            # 计算每个 worktree 相对主仓库的落后/超前提交数
            main_branch = None
            for wt in worktrees:
                if wt.is_main:
                    main_branch = wt.branch
                    break
            if main_branch and main_branch != "(detached)":
                for wt in worktrees:
                    if wt.is_main or wt.branch == "(detached)":
                        continue
                    try:
                        rev_result = _run_git(
                            ["rev-list", "--left-right", "--count",
                             f"{main_branch}...{wt.branch}"],
                            cwd=git_root,
                        )
                        if rev_result.returncode == 0:
                            parts = rev_result.stdout.strip().split("\t")
                            if len(parts) == 2:
                                # left=main_branch, right=worktree_branch
                                # behind = main 有而 worktree 没有
                                # ahead  = worktree 有而 main 没有
                                wt.behind_main = int(parts[0])
                                wt.ahead_main = int(parts[1])
                    except Exception:
                        pass
            
            return worktrees
            
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.debug(f"[GitWorktree] list_worktrees failed: {e}")
            return []

    @staticmethod
    def get_repo_info(path: str) -> Optional[GitRepoInfo]:
        """获取路径的完整 git 仓库信息（带 2s 缓存）"""
        if not path:
            return None
        if not GitWorktreeDetector._is_git_available():
            return None

        cached = GitWorktreeDetector._cache_get(GitWorktreeDetector._info_cache, path)
        if cached is not None:
            return cached

        git_root = GitWorktreeDetector.detect_git(path)
        if not git_root:
            GitWorktreeDetector._cache_set(GitWorktreeDetector._info_cache, path, None)
            return None
        
        is_wt = GitWorktreeDetector.is_worktree(path) or GitWorktreeDetector.is_worktree(git_root)
        branch = GitWorktreeDetector.get_current_branch(path)
        worktrees = GitWorktreeDetector.list_worktrees(git_root)
        
        info = GitRepoInfo(
            root=git_root,
            is_worktree=is_wt,
            worktrees=worktrees,
            current_branch=branch,
        )
        GitWorktreeDetector._cache_set(GitWorktreeDetector._info_cache, path, info)
        return info

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
    def get_worktree_by_branch(worktrees: List[WorktreeInfo], branch: str) -> Optional[WorktreeInfo]:
        """根据分支名查找 worktree"""
        for wt in worktrees:
            if wt.branch == branch:
                return wt
        return None

    @staticmethod
    def get_worktree_by_path(worktrees: List[WorktreeInfo], path: str) -> Optional[WorktreeInfo]:
        """根据路径查找 worktree"""
        normalized = os.path.normpath(path)
        for wt in worktrees:
            if os.path.normpath(wt.path) == normalized:
                return wt
        return None
