# -*- coding: utf-8 -*-
"""test_memory_entries.py — 全局记忆 v2 事件存储（ID / journal / 重放 / 冲突 / compact）。"""

import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_ENTRIES = _ROOT / "plugins" / "assistant_hub" / "core" / "memory" / "entries.py"

_spec = importlib.util.spec_from_file_location("test_memory_entries_mod", str(_ENTRIES))
entries = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("test_memory_entries_mod", entries)
_spec.loader.exec_module(entries)


def test_new_id_shape_and_uniqueness():
    ids = {entries.new_id() for _ in range(200)}
    assert len(ids) == 200
    for i in ids:
        assert i.startswith("m")
        assert len(i) == 7


def test_new_entry_requires_text_and_section():
    e = entries.new_entry("工具与环境", "Windows 换行坑", origin="user", source={"kind": "user_statement"})
    assert e["section"] == "工具与环境"
    assert e["text"] == "Windows 换行坑"
    assert e["rev"] == 1
    assert e["origin"] == "user"
    assert e["created_at"] == e["updated_at"]


def test_paths_live_under_entries_dir(tmp_path):
    assert entries.journal_path(tmp_path, "dev-a").parent.name == "journal"
    assert entries.journal_path(tmp_path, "dev-a").name == "dev-a.jsonl"
    assert entries.snapshot_path(tmp_path).name == "snapshot.jsonl"
    assert entries.legacy_path(tmp_path).name == "legacy.jsonl"


# ── T2 journal / 重放 ────────────────────────────────────


def test_append_and_replay_put(tmp_path):
    entry = entries.new_entry("工具与环境", "第一条", origin="user")
    ev = entries.append_event(tmp_path, "dev-a", {"op": "put", "id": entry["id"], "entry": entry})
    assert ev["seq"] == 1
    state = entries.replay(tmp_path)
    assert state["entries"][entry["id"]]["active"]["text"] == "第一条"
    assert state["entries"][entry["id"]]["state"] == "active"


def test_seq_increases_per_device(tmp_path):
    a = entries.new_entry("工具与环境", "a")
    b = entries.new_entry("工具与环境", "b")
    entries.append_event(tmp_path, "dev-a", {"op": "put", "id": a["id"], "entry": a})
    entries.append_event(tmp_path, "dev-a", {"op": "put", "id": b["id"], "entry": b})
    lines = [
        ln
        for ln in entries.journal_path(tmp_path, "dev-a").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    seqs = [json.loads(ln)["seq"] for ln in lines]
    assert seqs == [1, 2]


def test_corrupt_line_is_skipped(tmp_path):
    entry = entries.new_entry("工具与环境", "好行")
    entries.append_event(tmp_path, "dev-a", {"op": "put", "id": entry["id"], "entry": entry})
    p = entries.journal_path(tmp_path, "dev-a")
    with open(p, "a", encoding="utf-8") as f:
        f.write("{broken json\n")
    state = entries.replay(tmp_path)
    assert state["entries"][entry["id"]]["active"]["text"] == "好行"


# ── T3 更新 / 合并 / 软删 / 移动 ────────────────────────


def test_put_supersedes_previous_version(tmp_path):
    old = entries.new_entry("工具与环境", "旧说法")
    entries.append_event(tmp_path, "dev-a", {"op": "put", "id": old["id"], "entry": old})
    new = dict(old, text="新说法", rev=2, updated_at=entries.now_iso())
    entries.append_event(
        tmp_path, "dev-a", {"op": "put", "id": old["id"], "entry": new, "supersedes": old["id"]}
    )
    item = entries.replay(tmp_path)["entries"][old["id"]]
    assert item["active"]["text"] == "新说法"
    assert item["versions"][0]["text"] == "旧说法"


def test_merge_marks_sources_superseded(tmp_path):
    a = entries.new_entry("工具与环境", "坑 A")
    b = entries.new_entry("工具与环境", "坑 B")
    for e in (a, b):
        entries.append_event(tmp_path, "dev-a", {"op": "put", "id": e["id"], "entry": e})
    target = entries.new_entry("工具与环境", "坑 A 与 B 合并")
    entries.append_event(
        tmp_path,
        "dev-a",
        {"op": "merge", "id": target["id"], "entry": target, "sources": [a["id"], b["id"]]},
    )
    state = entries.replay(tmp_path)["entries"]
    assert state[a["id"]]["state"] == "superseded"
    assert state[b["id"]]["state"] == "superseded"
    assert state[target["id"]]["state"] == "active"


def test_delete_is_soft(tmp_path):
    e = entries.new_entry("用户偏好", "会被软删")
    entries.append_event(tmp_path, "dev-a", {"op": "put", "id": e["id"], "entry": e})
    entries.append_event(tmp_path, "dev-a", {"op": "del", "id": e["id"], "reason": "过时"})
    item = entries.replay(tmp_path)["entries"][e["id"]]
    assert item["state"] == "deleted"
    assert item["active"]["text"] == "会被软删"


def test_move_and_pin(tmp_path):
    e = entries.new_entry("工具与环境", "要搬家")
    entries.append_event(tmp_path, "dev-a", {"op": "put", "id": e["id"], "entry": e})
    entries.append_event(tmp_path, "dev-a", {"op": "move", "id": e["id"], "section": "用户偏好"})
    entries.append_event(tmp_path, "dev-a", {"op": "pin", "id": e["id"], "pinned": True})
    item = entries.replay(tmp_path)["entries"][e["id"]]
    assert item["active"]["section"] == "用户偏好"
    assert item["active"]["pinned"] is True


# ── T4 冲突 ──────────────────────────────────────────────


def _ev(entry, ts, device):
    return {"op": "put", "id": entry["id"], "entry": dict(entry, updated_at=ts), "ts": ts, "device": device}


def test_conflict_marks_and_keeps_both(tmp_path):
    e = entries.new_entry("工具与环境", "版本一")
    entries.append_event(tmp_path, "dev-a", _ev(e, "2026-11-19T10:00:00+08:00", "dev-a"))
    e2 = dict(e, text="版本二")
    entries.append_event(tmp_path, "dev-b", _ev(e2, "2026-11-19T11:00:00+08:00", "dev-b"))
    item = entries.replay(tmp_path)["entries"][e["id"]]
    assert item["conflict"] is True
    assert item["active"]["text"] == "版本二"
    assert item["versions"][-1]["text"] == "版本一"


def test_far_apart_updates_are_not_conflict(tmp_path):
    e = entries.new_entry("工具与环境", "版本一")
    entries.append_event(tmp_path, "dev-a", _ev(e, "2026-11-01T10:00:00+08:00", "dev-a"))
    e2 = dict(e, text="版本二")
    entries.append_event(tmp_path, "dev-b", _ev(e2, "2026-11-19T11:00:00+08:00", "dev-b"))
    item = entries.replay(tmp_path)["entries"][e["id"]]
    assert item["conflict"] is False


def test_active_entries_sorted_pinned_first(tmp_path):
    a = entries.new_entry("工具与环境", "普通")
    b = entries.new_entry("工具与环境", "置顶")
    entries.append_event(tmp_path, "dev-a", {"op": "put", "id": a["id"], "entry": a})
    entries.append_event(tmp_path, "dev-a", {"op": "put", "id": b["id"], "entry": b})
    entries.append_event(tmp_path, "dev-a", {"op": "pin", "id": b["id"], "pinned": True})
    state = entries.replay(tmp_path)
    ids = [e["id"] for e in entries.active_entries(state)]
    assert ids[0] == b["id"]


# ── T5 compact ───────────────────────────────────────────


def test_compact_preserves_state(tmp_path):
    e = entries.new_entry("工具与环境", "会被快照")
    entries.append_event(tmp_path, "dev-a", {"op": "put", "id": e["id"], "entry": e})
    before = entries.replay(tmp_path)
    entries.compact(tmp_path)
    after = entries.replay(tmp_path)
    assert after["entries"][e["id"]]["active"]["text"] == "会被快照"
    assert after["covered"]["dev-a"] == 1
    assert before["entries"].keys() == after["entries"].keys()


def test_replay_after_compact_ignores_covered(tmp_path):
    e = entries.new_entry("工具与环境", "快照后新增")
    entries.append_event(tmp_path, "dev-a", {"op": "put", "id": e["id"], "entry": e})
    entries.compact(tmp_path)
    e2 = entries.new_entry("工具与环境", "第二层")
    entries.append_event(tmp_path, "dev-a", {"op": "put", "id": e2["id"], "entry": e2})
    state = entries.replay(tmp_path)
    assert len(entries.active_entries(state)) == 2