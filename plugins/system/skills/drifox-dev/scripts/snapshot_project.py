"""drifox-dev 有状态技能的项目快照脚本。

周期性扫描项目，把"事实性"信息（文件行数、最近 commits、未提交变更）
写入 state.json 的 auto_snapshot 字段。AI 不必再手抄这些会过期的事实。

扫描内容：
- 关键文件行数（app/main_widget.py, app/core/backend.py 等）
- 最近 N 个 git commit（hash + 短消息 + 日期）
- 当前未提交变更（git status --porcelain）

用法：
    python -m plugins.system.skills.drifox-dev.scripts.snapshot_project
    python -m plugins.system.skills.drifox-dev.scripts.snapshot_project --json    # 只打印 JSON 不写
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# 关键文件列表（与 SKILL.md 中"3.3 关键文件大小参考"对应）
# 路径相对于 DriFox 项目根目录
KEY_FILES = [
    "app/main_widget.py",
    "app/core/backend.py",
    "app/core/workers/chat_worker.py",
    "app/core/hook_manager.py",
    "app/core/plugin_manager.py",
    "app/tools/__init__.py",
    "app/core/lsp/lsp_manager.py",
    "app/gateway/manager.py",
]

# 最多保留的最近 commit 数
MAX_RECENT_COMMITS = 10


def _run(cmd: list[str], cwd: Path, timeout: int = 10) -> tuple[int, str, str]:
    """运行子命令，返回 (rc, stdout, stderr)。"""
    if not shutil.which(cmd[0]):
        return (127, "", f"command not found: {cmd[0]}")
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd),
            capture_output=True, text=True, timeout=timeout,
        )
        return (proc.returncode, proc.stdout.strip(), proc.stderr.strip())
    except subprocess.TimeoutExpired:
        return (124, "", "timeout")


def find_project_root(start: Path) -> Path | None:
    """从 start 向上查找带 .git 的项目根。"""
    cur = start.resolve()
    for _ in range(20):
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None


def count_lines(filepath: Path) -> int | None:
    """统计文件行数。文件不存在或读取失败返回 None。"""
    if not filepath.exists() or not filepath.is_file():
        return None
    try:
        with open(filepath, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return None


def get_key_files_lines(project_root: Path) -> dict[str, int | None]:
    return {p: count_lines(project_root / p) for p in KEY_FILES}


def get_recent_commits(project_root: Path, n: int = MAX_RECENT_COMMITS) -> list[dict]:
    """git log --oneline -n 解析为结构化数据。"""
    rc, out, _ = _run(
        ["git", "log", f"-n{n}", "--pretty=format:%H%x09%h%x09%aI%x09%s"],
        cwd=project_root,
    )
    if rc != 0 or not out:
        return []
    commits = []
    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        full, short, date, message = parts
        commits.append({
            "hash": short,
            "date": date,
            "message": message[:200],
        })
    return commits


def get_uncommitted(project_root: Path) -> dict:
    """git status --porcelain 解析。"""
    rc, out, _ = _run(["git", "status", "--porcelain"], cwd=project_root)
    if rc != 0:
        return {"dirty": False, "files": [], "error": out or "git status failed"}
    files = [ln[3:] for ln in out.splitlines() if len(ln) >= 4]
    return {
        "dirty": bool(files),
        "files": files[:50],  # 限制最多 50 条
        "total": len(files),
    }


def get_branch(project_root: Path) -> str | None:
    rc, out, _ = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=project_root)
    if rc == 0 and out:
        return out
    return None


def take_snapshot(project_root: Path | None = None) -> dict:
    """采集一次完整快照（不写入 state.json）。"""
    if project_root is None:
        # 默认从技能目录向上找项目根
        project_root = find_project_root(Path(__file__).resolve().parents[4])
    if project_root is None:
        return {
            "last_updated": datetime.now().isoformat(timespec="seconds"),
            "key_files_lines": {},
            "recent_commits": [],
            "uncommitted_changes": {"dirty": False, "files": []},
            "branch": None,
            "error": "未找到 DriFox 项目根目录（无 .git）",
        }

    snapshot = {
        "last_updated": datetime.now().isoformat(timespec="seconds"),
        "key_files_lines": get_key_files_lines(project_root),
        "recent_commits": get_recent_commits(project_root),
        "uncommitted_changes": get_uncommitted(project_root),
        "branch": get_branch(project_root),
    }
    return snapshot


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="采集 DriFox 项目快照并写入 state.json")
    p.add_argument("--json", action="store_true", help="只打印 JSON，不写入 state.json")
    p.add_argument("--project-root", help="手动指定 DriFox 项目根目录")
    args = p.parse_args(argv)

    project_root = Path(args.project_root) if args.project_root else None
    snapshot = take_snapshot(project_root)

    print(json.dumps(snapshot, ensure_ascii=False, indent=2))

    if args.json:
        return 0

    # 写入 state.json（兼容 python -m 模式：sys.path[0] 是 cwd，需手动注入 scripts/）
    _scripts_dir = str(Path(__file__).resolve().parent)
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    from state_manager import update_snapshot  # type: ignore[import-not-found]
    update_snapshot(snapshot)
    print(f"\n[snapshot_project] 已更新 state.json 的 auto_snapshot 字段", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
