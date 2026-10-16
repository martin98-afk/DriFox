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

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# 项目记忆目录名（工作目录内）
PROJECT_DIR_NAME = ".drifox"
MEMORY_SUBDIR = "memory"
GLOBAL_MEMORY_FILE = "MEMORY.md"
PROJECT_MEMORY_FILE = "MEMORY.md"
# 已蒸馏日志后缀：YYYY-MM-DD.md → YYYY-MM-DD.distilled.md（内容保留可查，蒸馏/门禁时忽略）
DISTILLED_SUFFIX = ".distilled"

# ── 快照（覆盖写前的历史版本，集中存助手目录，随 Gitee 同步走）──────
SNAPSHOT_DIR_NAME = "memory_snapshots"
SNAPSHOT_KEEP = 10
WORKDIR_STATE_FILE = "workdir_state.json"

_SNAP_NAME_RE = re.compile(r"^\d{8}-\d{6}\.md$")

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


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 工作日志条目前缀的合法等级（对齐 loguru 命名）
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


def is_date_name(stem: str) -> bool:
    """文件名是否标准日期（YYYY-MM-DD）；同时是 date 参数的穿越防线。"""
    return bool(_DATE_RE.match(stem))


def parse_daily_stem(stem: str) -> Optional[tuple]:
    """日志文件 stem → (date, distilled)；非日志文件返回 None。

    识别两种形态：`YYYY-MM-DD`（未蒸馏）与 `YYYY-MM-DD.distilled`（已蒸馏）。
    """
    if is_date_name(stem):
        return stem, False
    if stem.endswith(DISTILLED_SUFFIX) and is_date_name(stem[: -len(DISTILLED_SUFFIX)]):
        return stem[: -len(DISTILLED_SUFFIX)], True
    return None


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
    """当日日志文件路径；date 缺省今天（YYYY-MM-DD）。

    date 必须是标准日期格式：它会被拼进文件名，而 memory_write 是
    danger=safe 工具（不弹审批），不校验就是沙箱穿越向量。
    """
    d = date or datetime.now().strftime("%Y-%m-%d")
    if not is_date_name(d):
        raise ValueError(f"非法日志日期: {d!r}（须为 YYYY-MM-DD）")
    return project_dir(workdir, aid) / f"{d}.md"


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def distilled_daily_path(path: Path) -> Path:
    """未蒸馏日志路径 → 已蒸馏路径（YYYY-MM-DD.md → YYYY-MM-DD.distilled.md）。"""
    return Path(path).parent / (Path(path).stem + DISTILLED_SUFFIX + Path(path).suffix)


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
    """读某天日志全文；未蒸馏版缺失时回落已蒸馏版（蒸馏后仍可查）。"""
    if not workdir:
        return ""
    p = daily_path(workdir, aid, date)
    text = read_text(p)
    if not text.strip():
        text = read_text(distilled_daily_path(p))
    return text


def read_daily_paged(workdir: str | Path, aid: str, date: str = "", page: int = 1, per_page: int = 20) -> Dict:
    """单日日志条目分页读取（防长日志整块砸上下文）。

    按空行切段（条目流天然边界），返回 {ok, content, page, pages, total, distilled}；
    无日志时 {ok: False, error: "not_found"}。
    """
    if not workdir:
        return {"ok": False, "error": "no_workdir"}
    p = daily_path(workdir, aid, date)
    raw = read_text(p)
    distilled = False
    if not raw.strip():
        dp = distilled_daily_path(p)
        raw = read_text(dp)
        distilled = dp.exists() and bool(raw.strip())
    if not raw.strip():
        return {"ok": False, "error": "not_found"}
    entries = [e.strip() for e in re.split(r"\n\s*\n", raw) if e.strip()]
    total = len(entries)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(int(page or 1), 1), pages)
    start = (page - 1) * per_page
    return {
        "ok": True,
        "content": "\n\n".join(entries[start : start + per_page]),
        "page": page,
        "pages": pages,
        "total": total,
        "distilled": distilled,
    }


def list_daily(workdir: str | Path, aid: str, limit: int = DAILY_LOOKBACK) -> List[Dict]:
    """近期日志列表（新→旧）：[{"date", "chars", "distilled"}...]；已蒸馏仍列出。"""
    if not workdir:
        return []
    d = project_dir(workdir, aid)
    if not d.exists():
        return []
    out: List[Dict] = []
    for f in sorted(d.glob("*.md"), key=lambda p: p.name, reverse=True):
        parsed = parse_daily_stem(f.stem)
        if f.name == PROJECT_MEMORY_FILE or parsed is None:
            continue  # MEMORY.md 与用户杂项不进日志索引
        date, distilled = parsed
        try:
            size = len(f.read_text(encoding="utf-8").strip())
        except Exception:
            continue
        if size <= 0:
            continue
        out.append({"date": date, "chars": size, "distilled": distilled})
        if len(out) >= max(limit, 0):
            break
    return out


def stale_daily(workdir: str | Path, aid: str, days: int = STALE_DAILY_DAYS) -> List[str]:
    """超过 days 天且**未蒸馏**的日志日期（蒸馏候选，供注入侧 ACTION 提示）。"""
    if not workdir:
        return []
    d = project_dir(workdir, aid)
    if not d.exists():
        return []
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out: List[str] = []
    for f in sorted(d.glob("*.md")):
        parsed = parse_daily_stem(f.stem)
        if f.name == PROJECT_MEMORY_FILE or parsed is None:
            continue  # 只认标准日期名，其余当用户杂项跳过
        stem, distilled = parsed
        if distilled:
            continue  # 已蒸馏不再是候选
        if stem < cutoff:
            out.append(stem)
    return out


def daily_exists(workdir: str | Path, aid: str, date: str = "") -> bool:
    """某天日志是否已有内容（门禁判据：今天是否已写过）。"""
    if not workdir:
        return False
    try:
        p = daily_path(workdir, aid, date)
        return p.exists() and p.stat().st_size > 0
    except ValueError, OSError:
        return False


def undistilled_before(workdir: str | Path, aid: str, before: str = "") -> List[str]:
    """before（YYYY-MM-DD，缺省今天）之前全部未蒸馏日志日期，旧→新（门禁蒸馏清单）。"""
    if not workdir:
        return []
    d = project_dir(workdir, aid)
    if not d.exists():
        return []
    b = before or today()
    out: List[str] = []
    for f in sorted(d.glob("*.md")):
        parsed = parse_daily_stem(f.stem)
        if f.name == PROJECT_MEMORY_FILE or parsed is None:
            continue
        stem, distilled = parsed
        if not distilled and stem < b:
            out.append(stem)
    return out


def mark_distilled(workdir: str | Path, aid: str, date: str) -> Dict:
    """把 YYYY-MM-DD.md 重命名为 YYYY-MM-DD.distilled.md（保留可查，蒸馏时忽略）。"""
    if not workdir:
        return {"ok": False, "error": "no_workdir"}
    if not is_date_name(date or ""):
        return {"ok": False, "error": "bad_date"}
    p = daily_path(workdir, aid, date)
    with _lock_for(p):
        target = distilled_daily_path(p)
        if target.exists():
            return {"ok": False, "error": "already"}
        if not p.exists():
            return {"ok": False, "error": "not_found"}
        try:
            os.replace(p, target)
        except OSError as e:
            return {"ok": False, "error": str(e)}
    return {"ok": True, "date": date, "file": str(target)}


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


def append_daily(
    workdir: str | Path, aid: str, text: str, date: str = "", level: str = "INFO", summary: str = ""
) -> Dict:
    """追加一条到当日日志（append-only；条目间空行分隔）。

    程序给每条记录打 loguru 风格前缀：`YYYY-MM-DD HH:MM:SS | LEVEL | 摘要 | 细节`。
    时刻与等级是精确字段，由程序生成；模型提供 summary（一句话，便于浏览）与
    text（细节）；level 非法回落 INFO，summary 缺省时退化为三段式。
    """
    if not workdir:
        return {"ok": False, "error": "no_workdir"}
    body = (text or "").strip()
    if not body:
        return {"ok": False, "error": "empty"}
    lvl = str(level or "INFO").strip().upper()
    if lvl not in _LOG_LEVELS:
        lvl = "INFO"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    head = f"{stamp} | {lvl} | "
    s = (summary or "").strip()
    if s:
        head += f"{s.splitlines()[0].strip()} | "
    path = daily_path(workdir, aid, date)
    with _lock_for(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists() or path.stat().st_size == 0
        with open(path, "a", encoding="utf-8") as f:
            if not is_new:
                f.write("\n")  # 条目间空行分隔（Markdown 段落边界）
            f.write(head + body + "\n")
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
    lines = ["近期工作日志（读某天全文加 date=YYYY-MM-DD，长日志自动分页、翻页加 page=N）："]
    for it in items:
        tag = " · 已蒸馏" if it.get("distilled") else ""
        lines.append(f"- {it['date']}（{it['chars']} 字{tag}）")
    return "\n".join(lines)


# ── 快照 ─────────────────────────────────────────────────


def _workdir_key(workdir: str) -> str:
    """workdir → 稳定短哈希（项目快照组目录名，Windows 大小写不敏感）。"""
    norm = os.path.normcase(os.path.normpath(str(workdir)))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]


def snapshot_dir(aid_dir: Path, kind: str, workdir: str = "") -> Path:
    """快照组目录：global 固定组；project 按工作目录哈希分组。非法参数抛 ValueError。"""
    if kind == "global":
        return Path(aid_dir) / SNAPSHOT_DIR_NAME / "global"
    if kind == "project" and workdir:
        return Path(aid_dir) / SNAPSHOT_DIR_NAME / f"project_{_workdir_key(workdir)}"
    raise ValueError(f"非法快照组: kind={kind!r} workdir={workdir!r}")


def _prune_snapshots(sdir: Path) -> None:
    """按文件名（即时间）排序裁剪，只删合法快照名，origin.json 等不动。"""
    snaps = sorted(p for p in sdir.glob("*.md") if _SNAP_NAME_RE.match(p.name))
    for p in snaps[: max(len(snaps) - SNAPSHOT_KEEP, 0)]:
        try:
            p.unlink()
        except OSError:
            pass


def snapshot_memory(aid_dir: Path, kind: str, source: Path, workdir: str = "") -> Dict:
    """把 source 文件快照入组；源缺失/空白不快照；异常吞掉返回 ok=False（不得阻断主写）。"""
    try:
        src = Path(source)
        if not src.exists() or src.stat().st_size <= 0:
            return {"ok": False, "error": "no_source"}
        text = src.read_text(encoding="utf-8")
        if not text.strip():
            return {"ok": False, "error": "empty_source"}
        sdir = snapshot_dir(aid_dir, kind, workdir)
        sdir.mkdir(parents=True, exist_ok=True)
        if kind == "project":
            origin = sdir / "origin.json"
            if not origin.exists():
                origin.write_text(json.dumps({"workdir": str(workdir)}, ensure_ascii=False), encoding="utf-8")
        name = datetime.now().strftime("%Y%m%d-%H%M%S") + ".md"
        while (sdir / name).exists():  # 同秒冲突 +1s，保持名格式即时间序
            dt = datetime.strptime(name[:15], "%Y%m%d-%H%M%S") + timedelta(seconds=1)
            name = dt.strftime("%Y%m%d-%H%M%S") + ".md"
        _atomic_write(sdir / name, text)
        _prune_snapshots(sdir)
        return {"ok": True, "file": name}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_snapshots(aid_dir: Path, kind: str, workdir: str = "") -> List[Dict]:
    """快照列表（新→旧）：[{"file", "ts", "chars"}]；非法组或无目录返回 []。"""
    try:
        sdir = snapshot_dir(aid_dir, kind, workdir)
    except ValueError:
        return []
    if not sdir.exists():
        return []
    out: List[Dict] = []
    for p in sorted((q for q in sdir.glob("*.md") if _SNAP_NAME_RE.match(q.name)), reverse=True):
        ts = f"{p.name[0:4]}-{p.name[4:6]}-{p.name[6:8]} {p.name[9:11]}:{p.name[11:13]}:{p.name[13:15]}"
        out.append({"file": p.name, "ts": ts, "chars": len(read_text(p).strip())})
    return out


def read_snapshot(aid_dir: Path, kind: str, name: str, workdir: str = "") -> str:
    """读快照全文；name 仅接受 YYYYMMDD-HHMMSS.md（防穿越），非法抛 ValueError。"""
    if not _SNAP_NAME_RE.match(name or ""):
        raise ValueError(f"非法快照名: {name!r}")
    return read_text(snapshot_dir(aid_dir, kind, workdir) / name)


def restore_snapshot(aid_dir: Path, kind: str, name: str, target: Path, workdir: str = "") -> Dict:
    """恢复快照到 target；恢复前 target 现有内容先入快照（可反悔），原子写回。"""
    content = read_snapshot(aid_dir, kind, name, workdir)
    tgt = Path(target)
    backup = ""
    if tgt.exists() and tgt.stat().st_size > 0:
        r = snapshot_memory(aid_dir, kind, tgt, workdir)
        backup = str(r.get("file") or "")
    _atomic_write(tgt, content)
    return {"ok": True, "restored_from": name, "backup": backup}
