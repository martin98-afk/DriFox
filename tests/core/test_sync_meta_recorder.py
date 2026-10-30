# -*- coding: utf-8 -*-
"""同步元数据记账器回归"""

import json

from app.core.sync.meta_recorder import SyncMetaRecorder, load_state, merge_clock


CONFIG = {
    "UI": {"FontSize": "12"},
    "LLM": {"SavedProviders": {"p1": {"name": "x"}}},
    "MCP": {"Servers": [{"name": "a", "cmd": "1"}]},
}


def _recorder(config=None):
    r = SyncMetaRecorder()
    r.attach(config if config is not None else CONFIG, "{}")
    r.seed_missing()
    return r


def test_seed_missing_creates_entries_for_all_items():
    r = _recorder()
    entries = r.entries()
    assert "UI.FontSize" in entries
    assert "LLM.SavedProviders.p1" in entries
    assert "MCP.Servers.name:a" in entries
    # 集合本身不单独成条目
    assert "LLM.SavedProviders" not in entries


def test_meta_item_excluded_from_entries():
    """SyncMeta 自身不进条目表，否则每次 dump 都会改变自身 hash 导致时间戳自激"""
    r = SyncMetaRecorder()
    r.attach(
        {"System": {"SyncMeta": '{"clock":3}', "Other": 1}, "UI": {"FontSize": "12"}},
        '{"clock":3}',
    )
    r.seed_missing()
    assert "System.SyncMeta" not in r.entries()
    assert "System.Other" in r.entries()


def test_seed_missing_is_idempotent():
    r = _recorder()
    before = dict(r.entries())
    r.seed_missing()
    assert r.entries() == before


def test_scalar_change_bumps_ts():
    r = _recorder()
    old_ts = r.entries()["UI.FontSize"]["t"]
    r.on_item_changed("UI.FontSize", "12", "14")
    entry = r.entries()["UI.FontSize"]
    assert entry["t"] > old_ts
    assert not entry.get("d")
    assert r.values["UI.FontSize"] == "14"


def test_provider_added_only_new_entry_gets_touch():
    r = _recorder()
    p1_ts = r.entries()["LLM.SavedProviders.p1"]["t"]
    r.on_item_changed(
        "LLM.SavedProviders",
        {"p1": {"name": "x"}},
        {"p1": {"name": "x"}, "p2": {"name": "y"}},
    )
    entries = r.entries()
    assert entries["LLM.SavedProviders.p1"]["t"] == p1_ts
    assert entries["LLM.SavedProviders.p2"]["t"] > p1_ts
    assert not entries["LLM.SavedProviders.p2"].get("d")


def test_provider_removed_writes_tombstone():
    r = _recorder()
    r.on_item_changed(
        "LLM.SavedProviders",
        {"p1": {"name": "x"}, "p2": {"name": "y"}},
        {"p1": {"name": "x"}},
    )
    assert r.entries()["LLM.SavedProviders.p2"]["d"] is True


def test_list_element_removed_writes_tombstone():
    r = _recorder()
    r.on_item_changed("MCP.Servers", [{"name": "a"}, {"name": "b"}], [{"name": "a"}])
    assert r.entries()["MCP.Servers.name:b"]["d"] is True


def test_suppress_blocks_recording():
    r = _recorder()
    before = dict(r.entries())
    with r.suppress():
        r.on_item_changed("UI.FontSize", "12", "20")
    assert r.entries() == before
    assert r.values["UI.FontSize"] == "12"


def test_dump_and_reload_roundtrip():
    r = _recorder()
    r.on_item_changed("UI.FontSize", "12", "14")
    raw = r.dump()
    data = json.loads(raw)
    assert data["version"] == 1
    r2 = SyncMetaRecorder()
    r2.attach({"UI": {"FontSize": "14"}}, raw)
    assert r2.entries()["UI.FontSize"] == r.entries()["UI.FontSize"]
    assert r2.state["clock"] == r.state["clock"]


def test_attach_accepts_dict_and_garbage_state():
    r = SyncMetaRecorder()
    r.attach(CONFIG, {"entries": {"UI.FontSize": {"h": "x", "t": 5}}, "clock": 5})
    assert r.entries()["UI.FontSize"]["t"] == 5
    r2 = SyncMetaRecorder()
    r2.attach(CONFIG, "not json")
    assert r2.entries() == {}
    r2.seed_missing()
    assert "UI.FontSize" in r2.entries()


def test_merge_clock_takes_max():
    assert merge_clock({"clock": 3}, {"clock": 9}) == 9
    assert merge_clock("bad", {"clock": 2}) == 2


def test_replace_entries_keeps_clock_monotonic():
    r = _recorder()
    clock_before = int(r.state["clock"])
    r.replace_entries({"UI.FontSize": {"h": "z", "t": 1}}, 0)
    assert int(r.state["clock"]) >= clock_before
    assert load_state(r.dump())["entries"]["UI.FontSize"]["h"] == "z"
