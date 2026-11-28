# -*- coding: utf-8 -*-
"""显式压缩、跨设备重放与日志序号回归。"""
import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_ENTRIES = _ROOT / "plugins" / "assistant_hub" / "core" / "memory" / "entries.py"

_spec = importlib.util.spec_from_file_location("test_compact_entries", str(_ENTRIES))
entries = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("test_compact_entries", entries)
_spec.loader.exec_module(entries)


def _entry(text, section="工具与环境"):
    return entries.new_entry(section, text, origin="user")


def _put(aid_dir, device, text, section="工具与环境"):
    e = _entry(text, section)
    entries.append_event(aid_dir, device, {"op": "put", "id": e["id"], "entry": e})
    return e


def _texts(aid_dir):
    state = entries.replay(aid_dir)
    return {str(e.get("text") or "") for e in entries.active_entries(state)}


def test_large_journal_only_compacts_on_request(tmp_path):
    _put(tmp_path, "dev-a", "x" * 2_000_000)
    _put(tmp_path, "dev-a", "第二条")
    assert not list(entries.entries_dir(tmp_path).glob("snapshot*"))
    assert [e["seq"] for e in entries.read_events(tmp_path)] == [1, 2]

    entries.compact(tmp_path, keep_days=0, device="dev-a")
    assert entries.snapshot_path(tmp_path, "dev-a").is_file()
    assert len(_texts(tmp_path)) == 2


# ── 按设备分文件与合并读取 ────────────────────────────────


def test_two_devices_compact_independently_keep_both_entries(tmp_path):
    """两台设备各自 compact 并裁掉全部事件后，合并快照仍能读出双方条目。"""
    a = _put(tmp_path, "dev-a", "设备 A 的条目")
    entries.compact(tmp_path, keep_days=0, device="dev-a")
    assert entries.snapshot_path(tmp_path, "dev-a").exists()
    assert not any(entries._iter_jsonl(entries.journal_path(tmp_path, "dev-a")))

    b = _put(tmp_path, "dev-b", "设备 B 的条目")
    entries.compact(tmp_path, keep_days=0, device="dev-b")
    # 两台设备的 journal 都已清空，只剩两份快照
    assert _texts(tmp_path) == {"设备 A 的条目", "设备 B 的条目"}
    assert entries.replay(tmp_path)["entries"][a["id"]]["state"] == "active"
    assert entries.replay(tmp_path)["entries"][b["id"]]["state"] == "active"


def test_merge_keeps_deletion_over_older_update(tmp_path):
    """一台删除、另一台的旧版本不该把条目复活。"""
    e = _put(tmp_path, "dev-a", "要删的条目")
    entries.compact(tmp_path, keep_days=0, device="dev-a")
    entries.append_event(tmp_path, "dev-a", {"op": "del", "id": e["id"], "reason": "test"})
    entries.compact(tmp_path, keep_days=0, device="dev-b")
    assert entries.replay(tmp_path)["entries"][e["id"]]["state"] == "deleted"


def test_legacy_single_snapshot_file_still_readable(tmp_path):
    """旧版 snapshot.jsonl（无设备后缀）继续被合并读取，不需要迁移。"""
    payload = {
        "op": "snapshot",
        "ts": "2026-11-01T00:00:00+08:00",
        "covered": {"dev-old": 3},
        "entries": [{"id": "mlegacy", "state": "active", "active": {"text": "旧快照条目"}, "versions": []}],
        "legacy": [],
    }
    p = entries.entries_dir(tmp_path) / entries.SNAPSHOT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    state = entries.replay(tmp_path)
    assert state["entries"]["mlegacy"]["active"]["text"] == "旧快照条目"
    assert state["covered"]["dev-old"] == 3


# ── 变更指纹 ──────────────────────────────────────────────


def test_state_key_changes_after_write_and_stable_without(tmp_path):
    _put(tmp_path, "dev-a", "第一条")
    k1 = entries.state_key(tmp_path)
    assert entries.state_key(tmp_path) == k1
    _put(tmp_path, "dev-a", "第二条")
    assert entries.state_key(tmp_path) != k1

def test_simultaneous_append_and_manual_compact(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    start = Barrier(2)

    def append():
        start.wait()
        for i in range(50):
            _put(tmp_path, "dev-a", str(i))

    def compact():
        start.wait()
        for _ in range(10):
            entries.compact(tmp_path, keep_days=0, device="dev-a")

    with ThreadPoolExecutor(max_workers=2) as pool:
        writers = [pool.submit(append), pool.submit(compact)]
        for task in writers:
            task.result(timeout=10)
    assert _texts(tmp_path) == {str(i) for i in range(50)}
    assert sorted(ev["seq"] for ev in entries.read_events(tmp_path)) == list(range(1, 51))


def test_repeated_full_compact_preserves_sequence(tmp_path):
    _put(tmp_path, "dev-a", "第一条")
    entries.compact(tmp_path, keep_days=0)
    entries.compact(tmp_path, keep_days=0)
    _put(tmp_path, "dev-a", "下一条")
    assert list(entries._iter_jsonl(entries.journal_path(tmp_path, "dev-a")))[0]["seq"] == 2
    assert _texts(tmp_path) == {"第一条", "下一条"}


def test_offline_device_archives_replay_in_event_order(tmp_path):
    import shutil

    a, b = tmp_path / "a", tmp_path / "b"
    e = entries.new_entry("甲区", "正文")
    entries.append_event(a, "dev-a", {"op": "put", "id": e["id"], "entry": e, "ts": "2026-01-01"})
    entries.append_event(a, "dev-a", {"op": "move", "id": e["id"], "section": "乙区", "ts": "2026-01-03"})
    entries.append_event(b, "dev-b", {"op": "pin", "id": e["id"], "pinned": True, "ts": "2026-01-02"})
    for root, device in [(a, "dev-a"), (b, "dev-b")]:
        entries.compact(root, keep_days=0, device=device)
        assert not entries.journal_path(root, device).exists()
    shutil.copy2(entries.snapshot_path(a, "dev-a"), entries.snapshot_path(b, "dev-a"))
    item = entries.replay(b)["entries"][e["id"]]["active"]
    assert item["section"] == "乙区"
    assert item["pinned"] is True
