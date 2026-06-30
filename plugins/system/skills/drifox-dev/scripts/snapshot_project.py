"""drifox-dev 有状态技能的项目快照脚本。

周期性扫描项目，把"事实性"信息（文件行数、最近 commits、未提交变更、GitHub issues）
写入 state.json 的 auto_snapshot 字段。AI 不必再手抄这些会过期的事实。

扫描内容：
- 关键文件行数（app/main_widget.py, app/core/backend.py 等）
- 最近 N 个 git commit（hash + 短消息 + 日期）
- 当前未提交变更（git status --porcelain）
- GitHub 远程仓库 open issues（公开仓库，无需 token）

用法：
    python -m plugins.system.skills.drifox-dev.scripts.snapshot_project
    python -m plugins.system.skills.drifox-dev.scripts.snapshot_project --json    # 只打印 JSON 不写
    python ... --no-network                                            # 跳过 GitHub 拉取
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
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

# GitHub 配置
GITHUB_REPO = "martin98-afk/DriFox"
GITHUB_API_ISSUES = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
MAX_GITHUB_ISSUES = 10
GITHUB_TIMEOUT = 8  # 秒 — 防止离线时阻塞太久


def _run(cmd: list[str], cwd: Path, timeout: int = 10) -> tuple[int, str, str]:
    """运行子命令，返回 (rc, stdout, stderr)。

    显式指定 encoding='utf-8' 避免 Windows 默认 GBK 解码中文 commit 消息时崩溃。
    stdout/stderr 为 None 时（极少见，多见于子进程被信号杀）退化为空串。
    """
    if not shutil.which(cmd[0]):
        return (127, "", f"command not found: {cmd[0]}")
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout,
        )
        return (
            proc.returncode,
            (proc.stdout or "").strip(),
            (proc.stderr or "").strip(),
        )
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


def fetch_github_issues(limit: int = MAX_GITHUB_ISSUES) -> dict:
    """从 GitHub 拉取当前仓库最近 open issues。

    失败优雅降级：所有异常（超时 / 网络断开 / rate limit / 解析失败）都返回
    {"ok": False, "issues": [], "error": "..."}，不阻塞主流程。
    公开仓库不需要 token；priv 仓库需要 GITHUB_TOKEN 环境变量。
    """
    url = f"{GITHUB_API_ISSUES}?state=open&per_page={limit}&sort=created&direction=desc"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "drifox-dev-skill",
    }
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=GITHUB_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return {
            "ok": False,
            "issues": [],
            "error": f"network: {type(e).__name__}: {e}",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
    except json.JSONDecodeError as e:
        return {
            "ok": False,
            "issues": [],
            "error": f"json decode: {e}",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }

    issues = []
    for item in payload[:limit]:
        # GitHub 的 /issues 接口同时返回 PR，过滤掉（PR 含 pull_request 字段）
        if "pull_request" in item:
            continue
        issues.append({
            "number": item.get("number"),
            "title": (item.get("title") or "")[:200],
            "state": item.get("state"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "user": (item.get("user") or {}).get("login"),
            "labels": [lbl.get("name") for lbl in (item.get("labels") or [])][:5],
            "url": item.get("html_url"),
            "comments": item.get("comments", 0),
        })

    return {
        "ok": True,
        "issues": issues,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "repo": GITHUB_REPO,
        "count": len(issues),
    }


def take_snapshot(project_root: Path | None = None, *, fetch_network: bool = True) -> dict:
    """采集一次完整快照（不写入 state.json）。

    fetch_network=False 时跳过 GitHub issues 拉取（用于离线 / CI 环境）。
    """
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
            "github_issues": {"ok": False, "issues": [], "error": "no project root"},
        }

    snapshot = {
        "last_updated": datetime.now().isoformat(timespec="seconds"),
        "key_files_lines": get_key_files_lines(project_root),
        "recent_commits": get_recent_commits(project_root),
        "uncommitted_changes": get_uncommitted(project_root),
        "branch": get_branch(project_root),
        "github_issues": fetch_github_issues() if fetch_network else {
            "ok": False, "issues": [], "error": "skipped (--no-network)",
        },
    }
    return snapshot


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="采集 DriFox 项目快照并写入 state.json")
    p.add_argument("--json", action="store_true", help="只打印 JSON，不写入 state.json")
    p.add_argument("--project-root", help="手动指定 DriFox 项目根目录")
    p.add_argument(
        "--no-network",
        action="store_true",
        help="跳过 GitHub issues 拉取（离线 / CI 环境用）",
    )
    args = p.parse_args(argv)

    project_root = Path(args.project_root) if args.project_root else None
    snapshot = take_snapshot(project_root, fetch_network=not args.no_network)

    # 在网络错误时给一行 stderr 提示（不阻塞主流程）
    gi = snapshot.get("github_issues") or {}
    if not gi.get("ok") and gi.get("error"):
        print(
            f"[snapshot_project] GitHub issues 跳过: {gi['error'][:120]}",
            file=sys.stderr,
        )

    print(json.dumps(snapshot, ensure_ascii=False, indent=2))

    if args.json:
        return 0

    # 写入 state.json（兼容 python -m 模式：sys.path[0] 是 cwd，需手动注入 scripts/）
    _scripts_dir = str(Path(__file__).resolve().parent)
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    from state_manager import update_snapshot  # type: ignore[import-not-found]
    update_snapshot(snapshot)
    print("\n[snapshot_project] 已更新 state.json 的 auto_snapshot 字段", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
