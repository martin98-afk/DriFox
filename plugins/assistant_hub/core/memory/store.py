# -*- coding: utf-8 -*-
"""store.py — 记忆存储层（照抄 WorkBuddy 双层结构 · 每助手独立）。

布局（每助手一套）：
  全局记忆  <app_data>/assistant_hub/<aid>/MEMORY.md        跨项目；模型可读写
  项目记忆  {workdir}/.drifox/memory/<aid>/
            ├── YYYY-MM-DD.md   每日工作日志（append-only）
            └── MEMORY.md       项目长期笔记

并发：单实例架构（single_instance 保证），进程级 per-path 锁足够；
读容错返回空串，写用原子替换（tmp + os.replace）防半写。
本模块不 import 主程序，可被 hook 环境独立加载。
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# 项目记忆目录名（工作目录内）
PROJECT_DIR_NAME = ".drifox"
MEMORY_SUBDIR = "memory"
GLOBAL_MEMORY_FILE = "MEMORY.md"
PROJECT_MEMORY_FILE = "MEMORY.md"

# 注入相关内容量（manager/prompts 使用）
DAILY_LOOKBACK = 7  # 注入的近期日志条数
STALE_DAILY_DAYS = 30  # 超期未蒸馏阈值（天）
GLOBAL_MEMORY_MAX_CHARS = 4000
PROJECT_MEMORY_MAX_CHARS = 3000

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path))
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


# ── 路径 ─────────────────────────────────────────────────


def global_memory_path(aid_dir: Path) -> Path:
    """全局记忆文件路径（<助手目录>/MEMORY.md）。"""
    return Path(aid_dir) / GLOBAL_MEMORY_FILE


def project_dir(workdir: str | Path, aid: str) -> Path:
    """项目记忆目录（{workdir}/.drifox/memory/<aid>）。"""
    return Path(workdir) / PROJECT_DIR_NAME / MEMORY_SUBDIR / aid


def project_memory_path(workdir: str | Path, aid: str) -> Path:
    return project_dir(workdir, aid) / PROJECT_MEMORY_FILE


def daily_path(workdir: str | Path, aid: str, date: str = "") -> Path:
    """当日日志文件路径；date 缺省今天（YYYY-MM-DD）。"""
    d = date or datetime.now().strftime("%Y-%m-%d")
    return project_dir(workdir, aid) / f"{d}.md"


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ── 读 ───────────────────────────────────────────────────


def read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return ""


def read_global_memory(aid_dir: Path) -> str:
    return read_text(global_memory_path(aid_dir))


def read_project_memory(workdir: str | Path, aid: str) -> str:
    if not workdir:
        return ""
    return read_text(project_memory_path(workdir, aid))


def read_daily(workdir: str | Path, aid: str, date: str = "") -> str:
    if not workdir:
        return ""
    return read_text(daily_path(workdir, aid, date))


def list_daily(workdir: str | Path, aid: str, limit: int = DAILY_LOOKBACK) -> List[Dict]:
    """近期日志列表（新→旧）：[{"date", "chars"}...]。"""
    if not workdir:
        return []
    d = project_dir(workdir, aid)
    if not d.exists():
        return []
    out: List[Dict] = []
    for f in sorted(d.glob("*.md"), key=lambda p: p.name, reverse=True):
        if f.name == PROJECT_MEMORY_FILE:
            continue
        try:
            size = len(f.read_text(encoding="utf-8").strip())
        except Exception:
            continue
        if size <= 0:
            continue
        out.append({"date": f.stem, "chars": size})
        if len(out) >= max(limit, 0):
            break
    return out


def stale_daily(workdir: str | Path, aid: str, days: int = STALE_DAILY_DAYS) -> List[str]:
    """超过 days 天的日志日期列表（蒸馏候选，供注入侧 ACTION 提示）。"""
    if not workdir:
        return []
    d = project_dir(workdir, aid)
    if not d.exists():
        return []
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out: List[str] = []
    for f in sorted(d.glob("*.md")):
        if f.name == PROJECT_MEMORY_FILE:
            continue
        stem = f.stem
        # 只认标准日期名（YYYY-MM-DD），其余文件当用户杂项跳过
        if len(stem) != 10 or stem[4] != "-" or stem[7] != "-":
            continue
        if stem < cutoff:
            out.append(stem)
    return out


# ── 写 ───────────────────────────────────────────────────


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_global_memory(aid_dir: Path, content: str) -> bool:
    """整体替换全局记忆 MEMORY.md。"""
    path = global_memory_path(aid_dir)
    with _lock_for(path):
        _atomic_write(path, (content or "").rstrip() + "\n")
    return True


def write_project_memory(workdir: str | Path, aid: str, content: str) -> bool:
    """整体替换项目 MEMORY.md。"""
    if not workdir:
        return False
    path = project_memory_path(workdir, aid)
    with _lock_for(path):
        _atomic_write(path, (content or "").rstrip() + "\n")
    return True


def append_daily(workdir: str | Path, aid: str, text: str, date: str = "") -> Dict:
    """追加一条到当日日志（append-only；纯条目流，日期由文件名承载，条目间空行分隔）。"""
    if not workdir:
        return {"ok": False, "error": "no_workdir"}
    body = (text or "").strip()
    if not body:
        return {"ok": False, "error": "empty"}
    path = daily_path(workdir, aid, date)
    with _lock_for(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists() or path.stat().st_size == 0
        with open(path, "a", encoding="utf-8") as f:
            if not is_new:
                f.write("\n")  # 条目间空行分隔（Markdown 段落边界）
            f.write(body + "\n")
    try:
        chars = len(path.read_text(encoding="utf-8"))
    except Exception:
        chars = 0
    return {"ok": True, "file": str(path), "date": path.stem, "chars": chars}


def clear_memory(aid_dir: Path, workdir: str | Path = "") -> None:
    """清除该助手的全部记忆：全局 MEMORY.md + 项目记忆目录（含 MEMORY.md 与日志）。"""
    g = global_memory_path(aid_dir)
    with _lock_for(g):
        try:
            if g.exists():
                g.unlink()
        except OSError:
            pass
    if not workdir:
        return
    d = project_dir(workdir, Path(aid_dir).name)
    if not d.exists():
        return
    for f in d.glob("*.md"):
        lock = _lock_for(f)
        with lock:
            try:
                f.unlink()
            except OSError:
                pass


# ── 注入视图 ─────────────────────────────────────────────


def daily_index_text(workdir: str | Path, aid: str, limit: int = DAILY_LOOKBACK) -> str:
    """近期日志索引文本（注入用）；无日志返回空串。"""
    items = list_daily(workdir, aid, limit)
    if not items:
        return ""
    lines = ["近期工作日志（需要细节时用 memory_read scope=daily date=YYYY-MM-DD 读取）："]
    for it in items:
        lines.append(f"- {it['date']}（{it['chars']} 字）")
    return "\n".join(lines)
