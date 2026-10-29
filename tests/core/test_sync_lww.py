# -*- coding: utf-8 -*-
"""LWW 合并语义回归（LWW + 墓碑重写，见 docs/superpowers/plans/2026-10-29-sync-lww-rewrite.md）"""

from app.core.sync.lww import (
    COLLECTION_KEYS,
    LamportClock,
    entry_ts,
    is_tombstone,
    live,
    merge_config_lww,
    merge_entries,
    tombstone,
)


def test_remote_delete_propagates_when_local_untouched():
    """远端写墓碑，本地未改 → 条目删除"""
    local = {"p1": {"h": "aaa", "t": 3}}
    remote = {"p1": {"h": "aaa", "t": 4, "d": True}}
    merged, conflicts = merge_entries(local, remote)
    assert merged["p1"]["d"] is True
    assert conflicts == []


def test_local_delete_propagates():
    """本地写墓碑 → 合并结果墓碑胜出"""
    local = {"p1": {"h": "aaa", "t": 5, "d": True}}
    remote = {"p1": {"h": "aaa", "t": 3}}
    merged, _ = merge_entries(local, remote)
    assert merged["p1"]["d"] is True


def test_local_recreate_after_delete_wins_by_timestamp():
    """本地删除后又新建，t 更大 → 复活"""
    local = {"p1": {"h": "bbb", "t": 9}}
    remote = {"p1": {"h": "aaa", "t": 5, "d": True}}
    merged, _ = merge_entries(local, remote)
    assert merged["p1"].get("d") is None
    assert merged["p1"]["h"] == "bbb"


def test_remote_modify_beats_local_delete():
    """本地删了，但远端改过且 t 更大 → 修改胜出"""
    local = {"p1": {"h": "aaa", "t": 4, "d": True}}
    remote = {"p1": {"h": "ccc", "t": 7}}
    merged, _ = merge_entries(local, remote)
    assert merged["p1"]["h"] == "ccc"


def test_only_local_entry_kept():
    merged, _ = merge_entries({"a": {"h": "1", "t": 1}}, {})
    assert "a" in merged


def test_only_remote_entry_taken():
    merged, _ = merge_entries({}, {"b": {"h": "2", "t": 1}})
    assert "b" in merged


def test_same_ts_same_hash_no_conflict():
    merged, conflicts = merge_entries({"a": {"h": "1", "t": 2}}, {"a": {"h": "1", "t": 2}})
    assert conflicts == []


def test_same_ts_diff_hash_conflict_local_wins():
    merged, conflicts = merge_entries({"a": {"h": "1", "t": 2}}, {"a": {"h": "9", "t": 2}})
    assert merged["a"]["h"] == "1"
    assert conflicts == ["a"]


def test_lamport_observe_and_tick():
    c = LamportClock(0)
    assert c.tick() == 1
    c.observe(10)
    assert c.tick() == 11
    c.observe(3)
    assert c.tick() == 12


def test_entry_helpers():
    assert entry_ts(None) == -1
    assert entry_ts({"t": 4}) == 4
    assert entry_ts({"t": "bad"}) == 0
    assert is_tombstone(tombstone("h", 1)) is True
    assert is_tombstone(live("h", 1)) is False


# ── config 结构合并 ─────────────────────────────────────


def _cfg(**groups):
    return groups


def test_merge_remote_provider_delete_propagates():
    local = {"LLM": {"SavedProviders": {"p1": {"n": 1}, "p2": {"n": 2}}}}
    remote = {"LLM": {"SavedProviders": {"p1": {"n": 1}}}}
    le = {
        "LLM.SavedProviders.p1": {"h": "a", "t": 3},
        "LLM.SavedProviders.p2": {"h": "b", "t": 3},
    }
    re_ = {
        "LLM.SavedProviders.p1": {"h": "a", "t": 3},
        "LLM.SavedProviders.p2": {"h": "b", "t": 4, "d": True},
    }
    cfg, entries, conflicts = merge_config_lww(local, le, remote, re_)
    assert set(cfg["LLM"]["SavedProviders"]) == {"p1"}
    assert conflicts == []


def test_merge_local_provider_delete_propagates():
    local = {"LLM": {"SavedProviders": {"p1": {"n": 1}}}}
    remote = {"LLM": {"SavedProviders": {"p1": {"n": 1}, "p2": {"n": 2}}}}
    le = {
        "LLM.SavedProviders.p1": {"h": "a", "t": 3},
        "LLM.SavedProviders.p2": {"h": "b", "t": 6, "d": True},
    }
    re_ = {
        "LLM.SavedProviders.p1": {"h": "a", "t": 3},
        "LLM.SavedProviders.p2": {"h": "b", "t": 3},
    }
    cfg, _, _ = merge_config_lww(local, le, remote, re_)
    assert set(cfg["LLM"]["SavedProviders"]) == {"p1"}


def test_merge_both_devices_add_providers():
    local = {"LLM": {"SavedProviders": {"p1": {"n": 1}, "l1": {"n": 9}}}}
    remote = {"LLM": {"SavedProviders": {"p1": {"n": 1}, "r1": {"n": 8}}}}
    le = {
        "LLM.SavedProviders.p1": {"h": "a", "t": 3},
        "LLM.SavedProviders.l1": {"h": "l", "t": 4},
    }
    re_ = {
        "LLM.SavedProviders.p1": {"h": "a", "t": 3},
        "LLM.SavedProviders.r1": {"h": "r", "t": 5},
    }
    cfg, _, _ = merge_config_lww(local, le, remote, re_)
    assert set(cfg["LLM"]["SavedProviders"]) == {"p1", "l1", "r1"}


def test_merge_remote_newer_scalar_takes_remote_value():
    local = {"UI": {"FontSize": "12"}}
    remote = {"UI": {"FontSize": "16"}}
    cfg, _, _ = merge_config_lww(
        local, {"UI.FontSize": {"h": "x", "t": 2}}, remote, {"UI.FontSize": {"h": "y", "t": 7}}
    )
    assert cfg["UI"]["FontSize"] == "16"


def test_merge_local_newer_scalar_takes_local_value():
    local = {"UI": {"FontSize": "20"}}
    remote = {"UI": {"FontSize": "16"}}
    cfg, _, _ = merge_config_lww(
        local, {"UI.FontSize": {"h": "x", "t": 9}}, remote, {"UI.FontSize": {"h": "y", "t": 2}}
    )
    assert cfg["UI"]["FontSize"] == "20"


def test_merge_without_meta_falls_back_to_union():
    """元数据缺失（老版本抹掉 SyncMeta）→ 本地优先 union，不误删"""
    local = {"UI": {"FontSize": "12", "OnlyLocal": 1}}
    remote = {"UI": {"FontSize": "16", "OnlyRemote": 2}}
    cfg, _, _ = merge_config_lww(local, {}, remote, {})
    assert cfg["UI"]["FontSize"] == "12"
    assert cfg["UI"]["OnlyLocal"] == 1
    assert cfg["UI"]["OnlyRemote"] == 2


def test_merge_list_collection_by_element_key():
    local = {"MCP": {"Servers": [{"name": "a"}, {"name": "b"}]}}
    remote = {"MCP": {"Servers": [{"name": "a"}, {"name": "c"}]}}
    le = {
        "MCP.Servers.name:a": {"h": "1", "t": 2},
        "MCP.Servers.name:b": {"h": "2", "t": 2},
    }
    re_ = {
        "MCP.Servers.name:a": {"h": "1", "t": 2},
        "MCP.Servers.name:c": {"h": "3", "t": 2},
    }
    cfg, _, _ = merge_config_lww(local, le, remote, re_)
    assert [s["name"] for s in cfg["MCP"]["Servers"]] == ["a", "b", "c"]


def test_merge_unknown_collection_block_level():
    """未登记的 dict 型配置项整块 LWW，不做条目级拆分"""
    local = {"Future": {"Weird": {"x": 1}}}
    remote = {"Future": {"Weird": {"y": 2}}}
    cfg, entries, _ = merge_config_lww(
        local, {"Future.Weird": {"h": "l", "t": 2}}, remote, {"Future.Weird": {"h": "r", "t": 5}}
    )
    assert cfg["Future"]["Weird"] == {"y": 2}
    assert "Future.Weird" in entries


def test_merge_empty_groups_dropped():
    """合并后整组没有条目 → 不输出空分组"""
    local = {"LLM": {"SavedProviders": {}}}
    remote = {"LLM": {"SavedProviders": {}}}
    le = {"LLM.SavedProviders.p1": {"h": "a", "t": 9}}
    re_ = {"LLM.SavedProviders.p1": {"h": "a", "t": 3}}
    cfg, _, _ = merge_config_lww(local, le, remote, re_)
    assert cfg["LLM"]["SavedProviders"] == {}


def test_collection_keys_cover_real_collections():
    for key in (
        "LLM.SavedProviders",
        "LLM.ModelOverrides",
        "Tools.Toggles",
        "Tools.PermissionPolicy",
        "MCP.Servers",
        "Plugin.EnabledPlugins",
        "Plugin.DisabledPlugins",
        "Plugin.DisabledComponents",
        "LLM.EnabledSkills",
        "Plugin.ConfirmedPluginServers",
        "Marketplace.AllowedGitHosts",
        "Hooks.SafePythonModules",
        "QFluentWidgets.FontFamilies",
    ):
        assert key in COLLECTION_KEYS


# ── Settings.sync_meta ──────────────────────────────────


def test_sync_meta_item_exists_and_roundtrips():
    from app.utils.config import Settings
    from qfluentwidgets.common.config import ConfigItem

    item = getattr(Settings, "sync_meta", None)
    assert isinstance(item, ConfigItem)
    assert item.group == "System"
    assert item.name == "SyncMeta"
    d = Settings.get_instance().toDict()
    assert "SyncMeta" in d.get("System", {})


# ── 目录条目 ────────────────────────────────────────────


def test_reconcile_dir_entries_marks_new_and_changed():
    from app.core.sync.lww import reconcile_dir_entries

    clock = LamportClock(0)
    out = reconcile_dir_entries({"a.txt": "h1", "b.txt": "h2"}, {}, clock)
    assert out["a.txt"]["h"] == "h1"
    assert out["a.txt"]["t"] == 1
    assert out["b.txt"]["t"] == 2

    out2 = reconcile_dir_entries({"a.txt": "h-new", "b.txt": "h2"}, out, clock)
    assert out2["a.txt"]["h"] == "h-new"
    assert out2["a.txt"]["t"] == 3
    assert out2["b.txt"]["t"] == 2  # 未变化不刷新


def test_reconcile_dir_entries_writes_tombstone_for_removed_file():
    from app.core.sync.lww import reconcile_dir_entries

    clock = LamportClock(0)
    entries = reconcile_dir_entries({"a.txt": "h1"}, {}, clock)
    out = reconcile_dir_entries({}, entries, clock)
    assert out["a.txt"]["d"] is True
    assert out["a.txt"]["h"] == "h1"


def test_reconcile_dir_entries_keeps_tombstone_ts():
    from app.core.sync.lww import reconcile_dir_entries

    clock = LamportClock(0)
    entries = reconcile_dir_entries({"a.txt": "h1"}, {}, clock)
    out = reconcile_dir_entries({}, entries, clock)
    ts = out["a.txt"]["t"]
    out2 = reconcile_dir_entries({}, out, clock)
    assert out2["a.txt"]["t"] == ts  # 墓碑不因本地空转反复刷新


def test_seed_entries_from_manifest_uses_ts_zero():
    from app.core.sync.lww import seed_entries_from_manifest

    out = seed_entries_from_manifest({"a.txt": "h1"}, {})
    assert out["a.txt"] == {"h": "h1", "t": 0}
    kept = seed_entries_from_manifest({"a.txt": "h1"}, {"a.txt": {"h": "h1", "t": 7}})
    assert kept["a.txt"]["t"] == 7
