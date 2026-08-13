# -*- coding: utf-8 -*-
"""project-dashboard 数据采集 — git 统计 + 文件系统扫描

纯 stdlib，不依赖主程序模块，可独立测试。
"""
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

# 跳过目录：噪音目录不统计
_SKIP_DIRS = {
    ".git", ".drifox", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".idea", ".vscode", "target", "vendor",
}
# 扩展名 → 语言名（覆盖常见项目；未知扩展名归入原扩展名）
_EXT_LANG = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".jsx": "JavaScript", ".java": "Java", ".go": "Go", ".rs": "Rust",
    ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".hpp": "C++",
    ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
    ".kt": "Kotlin", ".scala": "Scala", ".md": "Markdown", ".rst": "Markdown",
    ".html": "HTML", ".css": "CSS", ".scss": "SCSS", ".json": "JSON",
    ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".xml": "XML",
    ".sql": "SQL", ".sh": "Shell", ".bat": "Batch", ".ps1": "PowerShell",
    ".dockerfile": "Dockerfile", ".txt": "Text",
}


def _run_git(cwd: str, *args: str, timeout: int = 8) -> str:
    """执行 git 命令，失败/超时返回空串（不抛异常）"""
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def find_git_root(start: str) -> Optional[str]:
    """从 start 向上查找 git 根目录；非 git 仓库返回 None"""
    out = _run_git(start, "rev-parse", "--show-toplevel")
    return out or None


def _scan_files(project_root: str):
    """遍历项目文件（跳过噪音目录），返回 (ext_counter, lang_files, lang_lines)"""
    ext_counter: Counter = Counter()
    lang_files: Counter = Counter()
    lang_lines: Counter = Counter()
    for dirpath, dirnames, filenames in os.walk(project_root):
        # 原地过滤跳过目录（os.walk 生效）
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            ext = Path(fn).suffix.lower()
            if not ext:
                continue
            ext_counter[ext] += 1
            lang = _EXT_LANG.get(ext, ext.lstrip(".").capitalize())
            lang_files[lang] += 1
            try:
                p = Path(dirpath) / fn
                if p.stat().st_size > 512 * 1024:  # 大文件跳过行数统计
                    continue
                with open(p, encoding="utf-8", errors="ignore") as f:
                    lang_lines[lang] += sum(1 for _ in f)
            except OSError:
                pass
    return ext_counter, lang_files, lang_lines


def collect_data(project_root: str) -> dict:
    """采集项目看板数据（git + 文件系统）

    Returns:
        dict: repo_name/branch/total_commits/daily_commits/contributors/
              languages/file_types/generated_at/error
    """
    result = {
        "repo_name": "", "branch": "", "total_commits": 0,
        "daily_commits": [], "contributors": [], "languages": [],
        "file_types": [], "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "error": None,
    }
    git_root = find_git_root(project_root)
    if not git_root:
        result["error"] = "当前目录不是 git 仓库"
        return result
    result["repo_name"] = Path(git_root).name
    result["branch"] = _run_git(git_root, "branch", "--show-current") or "unknown"

    # commit 趋势：近 30 天
    out = _run_git(
        git_root, "log", "--since=30 days ago", "--pretty=format:%ad",
        "--date=short",
    )
    daily: Counter = Counter()
    for line in out.splitlines():
        if line:
            daily[line] += 1
    result["daily_commits"] = sorted(daily.items())
    result["total_commits"] = sum(daily.values())

    # 贡献者 Top
    out = _run_git(git_root, "shortlog", "-sne", "--no-merges", "HEAD")
    for line in out.splitlines()[:8]:
        parts = line.strip().split("\t")
        if len(parts) == 2:
            try:
                result["contributors"].append((parts[1].strip(), int(parts[0])))
            except ValueError:
                pass

    # 文件统计
    ext_counter, lang_files, lang_lines = _scan_files(git_root)
    result["file_types"] = ext_counter.most_common(10)
    result["languages"] = sorted(
        ((lang, lang_files[lang], lang_lines[lang]) for lang in lang_files),
        key=lambda x: -x[1],
    )[:10]
    return result
