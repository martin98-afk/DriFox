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
from dataclasses import dataclass, field
from typing import List, Optional
from loguru import logger


# Windows 中文系统默认 GBK 编码，git 输出可能包含 UTF-8 字符
# 统一用 utf-8 + errors=replace 避免乱码崩溃
_SUBPROCESS_KWARGS = {
    "capture_output": True,
    "text": True,
    "encoding": "utf-8",
    "errors": "replace",
    "timeout": 5,
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


@dataclass
class GitRepoInfo:
    """Git 仓库信息"""
    root: str                # git 根目录（含 .git）
    is_worktree: bool        # 当前目录是否在 worktree 中
    worktrees: List[WorktreeInfo] = field(default_factory=list)
    current_branch: str = ""


class GitWorktreeDetector:
    """Git/Worktree 检测器"""

    @staticmethod
    def detect_git(path: str) -> Optional[str]:
        """
        检测路径是否在 git 仓库中
        
        Args:
            path: 要检测的路径
        
        Returns:
            Optional[str]: 如果是 git 仓库，返回 git 根目录路径；否则返回 None
        """
        if not path or not os.path.exists(path):
            return None
        
        try:
            result = _run_git(["rev-parse", "--show-toplevel"], cwd=path)
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.debug(f"[GitWorktree] detect_git failed for {path}: {e}")
        
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
            
            return worktrees
            
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.debug(f"[GitWorktree] list_worktrees failed: {e}")
            return []

    @staticmethod
    def get_repo_info(path: str) -> Optional[GitRepoInfo]:
        """
        获取路径的完整 git 仓库信息
        
        Args:
            path: 要检测的路径
        
        Returns:
            Optional[GitRepoInfo]: 仓库信息，非 git 仓库返回 None
        """
        git_root = GitWorktreeDetector.detect_git(path)
        if not git_root:
            return None
        
        is_wt = GitWorktreeDetector.is_worktree(path) or GitWorktreeDetector.is_worktree(git_root)
        branch = GitWorktreeDetector.get_current_branch(path)
        worktrees = GitWorktreeDetector.list_worktrees(git_root)
        
        return GitRepoInfo(
            root=git_root,
            is_worktree=is_wt,
            worktrees=worktrees,
            current_branch=branch,
        )

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
