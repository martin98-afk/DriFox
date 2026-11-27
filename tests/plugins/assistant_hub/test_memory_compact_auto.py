# -*- coding: utf-8 -*-
"""test_memory_compact_auto.py — 事件日志的自动压缩与多设备快照合并。

覆盖：阈值触发（低于阈值 / 间隔太近都不压）、按设备分文件写、合并读取不丢
另一台的条目、旧版单文件快照仍可读、state_key 随写入变化。
"""
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


# ── 阈值触发 ──────────────────────────────────────────────


def test_maybe_compact_skips_when_small(tmp_path):
    _put(tmp_path, "dev-a", "小日志")
    r = entries.maybe_compact(tmp_path, "dev-a")
    assert r["ok"] is False
    assert r["reason"] == "below_threshold"
    assert not list(entries.entries_dir(tmp_path).glob("snapshot*.jsonl"))


def test_maybe_compact_triggers_over_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(entries, "COMPACT_MIN_BYTES", 1)
    monkeypatch.setattr(entries, "COMPACT_FIRST_BYTES", 1)
    monkeypatch.setattr(entries, "COMPACT_MIN_INTERVAL_HOURS", 0)
    _put(tmp_path, "dev-a", "够大就压")
    r = entries.maybe_compact(tmp_path, "dev-a")
    assert r["ok"] is True
    assert entries.snapshot_path(tmp_path, "dev-a").exists()
    assert "够大就压" in _texts(tmp_path)


def test_maybe_compact_respects_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(entries, "COMPACT_MIN_BYTES", 1)
    monkeypatch.setattr(entries, "COMPACT_FIRST_BYTES", 1)
    monkeypatch.setattr(entries, "COMPACT_MIN_INTERVAL_HOURS", 12)
    entries.compact(tmp_path, keep_days=7, device="dev-a")
    _put(tmp_path, "dev-a", "刚压过")
    r = entries.maybe_compact(tmp_path, "dev-a")
    assert r["ok"] is False
    assert r["reason"] == "recent"


def test_first_compact_threshold_is_lower(tmp_path, monkeypatch):
    """尚无任何快照时用更低的首压门槛，避免 journal 长期没人压。"""
    monkeypatch.setattr(entries, "COMPACT_MIN_BYTES", 10**9)
    monkeypatch.setattr(entries, "COMPACT_FIRST_BYTES", 1)
    monkeypatch.setattr(entries, "COMPACT_MIN_INTERVAL_HOURS", 0)
    _put(tmp_path, "dev-a", "首次就该压")
    # 写入路径自己就会检查压缩条件，不需要外部再调一次
    assert entries.snapshot_path(tmp_path, "dev-a").exists()
    assert "首次就该压" in _texts(tmp_path)
    # 有快照后回到常规门槛（这里设得极大，所以不再触发）
    _put(tmp_path, "dev-a", "第二次")
    r = entries.maybe_compact(tmp_path, "dev-a")
    assert r["ok"] is False and r["reason"] == "below_threshold"


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