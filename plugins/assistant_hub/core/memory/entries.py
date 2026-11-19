"""entries.py — 全局记忆事件存储（append-only JSONL + 重放）。

不 import store / manager：外部信息全部由调用方传参，便于单测与热重载。
"""

from __future__ import annotations

import json
import os
import random
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = 1
ENTRIES_DIR = "entries"
JOURNAL_DIR = "journal"
SNAPSHOT_FILE = "snapshot.jsonl"
LEGACY_FILE = "legacy.jsonl"
VIEW_STATE_FILE = "view_state.json"
MIGRATED_FLAG = ".migrated_v2"

CONFLICT_WINDOW_HOURS = 24
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"

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


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


def entries_dir(aid_dir: Path) -> Path:
    return Path(aid_dir) / ENTRIES_DIR


def journal_dir(aid_dir: Path) -> Path:
    return entries_dir(aid_dir) / JOURNAL_DIR


def journal_path(aid_dir: Path, device: str) -> Path:
    return journal_dir(aid_dir) / f"{device}.jsonl"


def snapshot_path(aid_dir: Path) -> Path:
    return entries_dir(aid_dir) / SNAPSHOT_FILE


def legacy_path(aid_dir: Path) -> Path:
    return entries_dir(aid_dir) / LEGACY_FILE


def view_state_path(aid_dir: Path) -> Path:
    return entries_dir(aid_dir) / VIEW_STATE_FILE


def migrated_flag(aid_dir: Path) -> Path:
    return entries_dir(aid_dir) / MIGRATED_FLAG


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(existing: Iterable[str] = ()) -> str:
    seen = set(existing)
    while True:
        cand = "m" + "".join(random.choice(_ALPHABET) for _ in range(6))
        if cand not in seen:
            return cand


def new_entry(
    section: str,
    text: str,
    origin: str = "agent",
    source: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    stamp = now_iso()
    return {
        "id": new_id(),
        "section": str(section or "").strip(),
        "text": str(text or "").strip(),
        "origin": origin,
        "source": dict(source or {}),
        "created_at": stamp,
        "updated_at": stamp,
        "rev": 1,
        "pinned": False,
        "last_used_at": None,
        "use_count": 0,
    }


def load_or_create_device_id(app_data_dir: Path) -> str:
    """本机标识：放同步目录之外，避免两台设备写同一个 journal 文件。"""
    p = Path(app_data_dir) / "device_id"
    try:
        value = p.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    value = "dev-" + "".join(random.choice(_ALPHABET) for _ in range(8))
    _atomic_write(p, value + "\n")
    return value