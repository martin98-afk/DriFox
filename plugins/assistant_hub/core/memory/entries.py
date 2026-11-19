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


# ── journal 读写 ─────────────────────────────────────────


def _iter_jsonl(path: Path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _load_snapshot(aid_dir: Path) -> Optional[Dict[str, Any]]:
    for obj in _iter_jsonl(snapshot_path(aid_dir)):
        if obj.get("op") == "snapshot":
            return obj
    return None


def _covered(aid_dir: Path) -> Dict[str, int]:
    snap = _load_snapshot(aid_dir)
    return dict((snap or {}).get("covered") or {})


def _next_seq(aid_dir: Path, device: str) -> int:
    base = int(_covered(aid_dir).get(device, 0))
    last = 0
    for ev in _iter_jsonl(journal_path(aid_dir, device)):
        last = max(last, int(ev.get("seq") or 0))
    return max(base, last) + 1


def append_event(aid_dir: Path, device: str, event: Dict[str, Any]) -> Dict[str, Any]:
    p = journal_path(aid_dir, device)
    with _lock_for(p):
        ev = dict(event)
        ev.setdefault("schema", SCHEMA)
        ev.setdefault("ts", now_iso())
        ev["device"] = device
        ev["seq"] = _next_seq(aid_dir, device)
        line = json.dumps(ev, ensure_ascii=False, separators=(",", ":"))
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8", newline="") as f:
            f.write(line + "\n")
    return ev


def read_events(aid_dir: Path) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for p in sorted(journal_dir(aid_dir).glob("*.jsonl")):
        events.extend(_iter_jsonl(p))
    events.sort(key=lambda e: (str(e.get("ts") or ""), str(e.get("device") or ""), int(e.get("seq") or 0)))
    return events


# ── 状态机 ──────────────────────────────────────────────


def empty_state() -> Dict[str, Any]:
    return {"entries": {}, "legacy": [], "covered": {}}


def _state_item(entry_id: str) -> Dict[str, Any]:
    return {"id": entry_id, "versions": [], "active": None, "state": "active", "conflict": False}


def _apply_put(state: Dict[str, Any], ev: Dict[str, Any]) -> None:
    entry = ev.get("entry") or {}
    entry_id = str(ev.get("id") or entry.get("id") or "")
    if not entry_id:
        return
    item = state["entries"].setdefault(entry_id, _state_item(entry_id))
    if item["active"] is not None:
        item["versions"].append(item["active"])
    item["active"] = dict(entry)
    item["state"] = "active"
    sup = str(ev.get("supersedes") or "")
    if sup and sup != entry_id and sup in state["entries"]:
        state["entries"][sup]["state"] = "superseded"
        state["entries"][sup]["superseded_by"] = entry_id


def _apply_del(state: Dict[str, Any], ev: Dict[str, Any]) -> None:
    entry_id = str(ev.get("id") or "")
    item = state["entries"].get(entry_id)
    if item is None:
        return
    item["state"] = "deleted"
    item["deleted_at"] = str(ev.get("ts") or "")


def _apply_snapshot(state: Dict[str, Any], ev: Dict[str, Any]) -> None:
    for rec in ev.get("entries") or []:
        entry_id = str(rec.get("id") or "")
        if not entry_id:
            continue
        state["entries"][entry_id] = {
            "id": entry_id,
            "versions": list(rec.get("versions") or []),
            "active": rec.get("active"),
            "state": str(rec.get("state") or "active"),
            "conflict": bool(rec.get("conflict")),
        }
    state["legacy"] = list(ev.get("legacy") or [])
    state["covered"] = dict(ev.get("covered") or {})


def replay(aid_dir: Path) -> Dict[str, Any]:
    state = empty_state()
    snap = _load_snapshot(aid_dir)
    if snap:
        _apply_snapshot(state, snap)
    covered = state.get("covered") or {}
    for ev in read_events(aid_dir):
        device = str(ev.get("device") or "")
        if int(ev.get("seq") or 0) <= int(covered.get(device, 0)):
            continue
        op = ev.get("op")
        if op == "put":
            _apply_put(state, ev)
        elif op == "del":
            _apply_del(state, ev)
    return state