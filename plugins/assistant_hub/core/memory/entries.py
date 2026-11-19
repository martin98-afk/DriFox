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


def write_legacy(aid_dir: Path, items: List[Dict[str, Any]]) -> None:
    """迁移时无法解析的原始段落：整段保留，不参与注入与配额。"""
    lines = [json.dumps(x, ensure_ascii=False) for x in items]
    _atomic_write(legacy_path(aid_dir), "\n".join(lines) + "\n")


def fingerprint(aid_dir: Path) -> str:
    """journal + snapshot 的内容摘要：任一事件变化都会变，与视图渲染无关。"""
    import hashlib

    h = hashlib.sha1()
    for p in sorted(entries_dir(aid_dir).rglob("*.jsonl")):
        try:
            h.update(p.name.encode("utf-8"))
            h.update(p.read_bytes())
        except OSError:
            continue
    return "j" + h.hexdigest()[:12]


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


def _parse_ts(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _conflict(prev: Dict[str, Any], new: Dict[str, Any]) -> bool:
    t1, t2 = _parse_ts(prev.get("updated_at")), _parse_ts(new.get("updated_at"))
    if t1 is None or t2 is None:
        return False
    return abs((t1 - t2).total_seconds()) < CONFLICT_WINDOW_HOURS * 3600


def _apply_put(state: Dict[str, Any], ev: Dict[str, Any]) -> None:
    entry = ev.get("entry") or {}
    entry_id = str(ev.get("id") or entry.get("id") or "")
    if not entry_id:
        return
    item = state["entries"].setdefault(entry_id, _state_item(entry_id))
    prev = item["active"]
    explicit = str(ev.get("supersedes") or "") == entry_id
    if prev is not None:
        item["versions"].append(prev)
        if not explicit and _conflict(prev, entry):
            item["conflict"] = True
    item["active"] = dict(entry)
    item["state"] = "active"
    sup = str(ev.get("supersedes") or "")
    if sup and sup != entry_id and sup in state["entries"]:
        state["entries"][sup]["state"] = "superseded"
        state["entries"][sup]["superseded_by"] = entry_id


def active_entries(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = [
        dict(item["active"], id=item["id"])
        for item in state["entries"].values()
        if item.get("state") == "active" and item.get("active")
    ]
    out.sort(key=lambda e: (not bool(e.get("pinned")), str(e.get("updated_at") or "")))
    return out


def _apply_del(state: Dict[str, Any], ev: Dict[str, Any]) -> None:
    entry_id = str(ev.get("id") or "")
    item = state["entries"].get(entry_id)
    if item is None:
        return
    if item.get("active"):
        item["versions"].append(item["active"])
    item["state"] = "deleted"
    item["deleted_at"] = str(ev.get("ts") or "")


def _apply_merge(state: Dict[str, Any], ev: Dict[str, Any]) -> None:
    target = ev.get("entry") or {}
    target_id = str(ev.get("id") or target.get("id") or "")
    for sid in ev.get("sources") or []:
        src = state["entries"].get(str(sid))
        if src is not None and src["state"] == "active":
            src["state"] = "superseded"
            src["superseded_by"] = target_id
    _apply_put(state, {"op": "put", "id": target_id, "entry": target})


def _apply_move(state: Dict[str, Any], ev: Dict[str, Any]) -> None:
    item = state["entries"].get(str(ev.get("id") or ""))
    if item is None or not item.get("active"):
        return
    item["active"] = dict(
        item["active"], section=str(ev.get("section") or ""), updated_at=str(ev.get("ts") or "")
    )
    item["active"]["rev"] = int(item["active"].get("rev") or 1) + 1


def _apply_pin(state: Dict[str, Any], ev: Dict[str, Any]) -> None:
    item = state["entries"].get(str(ev.get("id") or ""))
    if item is None or not item.get("active"):
        return
    item["active"] = dict(item["active"], pinned=bool(ev.get("pinned")))


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
        elif op == "merge":
            _apply_merge(state, ev)
        elif op == "move":
            _apply_move(state, ev)
        elif op == "pin":
            _apply_pin(state, ev)
    _load_legacy(state, aid_dir)
    return state


def _load_legacy(state: Dict[str, Any], aid_dir: Path) -> None:
    """legacy.jsonl 是唯一来源；文件不存在时保留 snapshot 里的 legacy。"""
    p = legacy_path(aid_dir)
    if not p.exists():
        return
    state["legacy"] = []
    for obj in _iter_jsonl(p):
        state["legacy"].append(
            {
                "text": str(obj.get("text") or ""),
                "reason": str(obj.get("reason") or ""),
                "line_no": obj.get("line_no") or 0,
            }
        )


def compact(aid_dir: Path, keep_days: int = 7) -> Dict[str, Any]:
    """当前状态落成 snapshot，再裁剪旧事件；读取结果与裁剪前等价。"""
    state = replay(aid_dir)
    covered: Dict[str, int] = {}
    for p in journal_dir(aid_dir).glob("*.jsonl"):
        seqs = [int(ev.get("seq") or 0) for ev in _iter_jsonl(p)]
        covered[p.stem] = max(seqs or [0])
    payload = {
        "op": "snapshot",
        "ts": now_iso(),
        "covered": covered,
        "entries": [
            {
                "id": item["id"],
                "state": item["state"],
                "active": item.get("active"),
                "versions": item.get("versions") or [],
                "conflict": bool(item.get("conflict")),
            }
            for item in state["entries"].values()
        ],
        "legacy": state.get("legacy") or [],
    }
    _atomic_write(snapshot_path(aid_dir), json.dumps(payload, ensure_ascii=False) + "\n")
    _prune_journals(aid_dir, keep_days)
    return {"ok": True, "entries": len(payload["entries"]), "covered": covered}


def _prune_journals(aid_dir: Path, keep_days: int) -> None:
    """保留最近 keep_days 天的事件；时间解析失败的行一律保留（宁多留不误删）。"""
    cutoff = datetime.now().astimezone().timestamp() - keep_days * 86400
    for p in journal_dir(aid_dir).glob("*.jsonl"):
        keep: List[str] = []
        for ev in _iter_jsonl(p):
            ts = _parse_ts(ev.get("ts"))
            if ts is None or ts.timestamp() >= cutoff:
                keep.append(json.dumps(ev, ensure_ascii=False, separators=(",", ":")))
        if keep:
            _atomic_write(p, "\n".join(keep) + "\n")
        else:
            p.unlink(missing_ok=True)