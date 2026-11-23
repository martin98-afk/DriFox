# -*- coding: utf-8 -*-
"""test_memory_write_v2.py — 全局记忆 v2 读路径、迁移与写路径 diff。"""

import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_BASE = _ROOT / "plugins" / "assistant_hub" / "core" / "memory"

_entries_spec = importlib.util.spec_from_file_location("test_mw_entries", str(_BASE / "entries.py"))
entries = importlib.util.module_from_spec(_entries_spec)
sys.modules.setdefault("test_mw_entries", entries)
_entries_spec.loader.exec_module(entries)

_view_spec = importlib.util.spec_from_file_location("test_mw_view", str(_BASE / "view.py"))
view = importlib.util.module_from_spec(_view_spec)
sys.modules.setdefault("test_mw_view", view)
_view_spec.loader.exec_module(view)

_mig_spec = importlib.util.spec_from_file_location("test_mw_migrate", str(_BASE / "migrate.py"))
migrate = importlib.util.module_from_spec(_mig_spec)
sys.modules.setdefault("test_mw_migrate", migrate)
_mig_spec.loader.exec_module(migrate)

SECTIONS = [{"name": "工具与环境", "hint": "工具坑", "detail": "工具自身行为"}]


def _bootstrap(aid_dir: Path, text: str = "") -> None:
    """模拟 manager 的首次加载：迁移旧文本，写视图。"""
    if text:
        parsed = migrate.parse_legacy(text, SECTIONS)
        for ev in parsed["events"]:
            e = entries.new_entry(ev["section"], ev["text"], origin="import", source=ev["source"])
            entries.append_event(aid_dir, "dev-a", {"op": "put", "id": e["id"], "entry": e})
        if parsed["legacy"]:
            (aid_dir / "entries").mkdir(parents=True, exist_ok=True)
            entries.legacy_path(aid_dir).write_text(
                "\n".join(json.dumps(x, ensure_ascii=False) for x in parsed["legacy"]) + "\n",
                encoding="utf-8",
            )
    (aid_dir / "entries").mkdir(parents=True, exist_ok=True)
    entries.migrated_flag(aid_dir).write_text("1\n", encoding="utf-8")


def test_read_renders_view_after_migration(tmp_path):
    _bootstrap(tmp_path, "## 工具与环境\n- Windows 换行坑\n")
    state = entries.replay(tmp_path)
    md = view.render_markdown(state, SECTIONS)
    assert "Windows 换行坑" in md
    assert "## 工具与环境" in md


def test_legacy_file_is_loaded_into_state(tmp_path):
    _bootstrap(tmp_path)
    (tmp_path / "entries").mkdir(parents=True, exist_ok=True)
    entries.legacy_path(tmp_path).write_text(
        json.dumps({"text": "无处安放", "reason": "prose", "line_no": 2}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    state = entries.replay(tmp_path)
    assert state["legacy"][0]["text"] == "无处安放"


# ── T10 写路径 diff ──────────────────────────────────────


def test_append_creates_entry_and_updates_view(tmp_path):
    _bootstrap(tmp_path)
    e = entries.new_entry("工具与环境", "新条目")
    entries.append_event(tmp_path, "dev-a", {"op": "put", "id": e["id"], "entry": e})
    md = view.render_markdown(entries.replay(tmp_path), SECTIONS)
    assert "新条目" in md


def test_delete_by_id_is_soft_and_recoverable(tmp_path):
    e = entries.new_entry("工具与环境", "会删的")
    entries.append_event(tmp_path, "dev-a", {"op": "put", "id": e["id"], "entry": e})
    entries.append_event(tmp_path, "dev-a", {"op": "del", "id": e["id"], "reason": "过时"})
    state = entries.replay(tmp_path)
    assert entries.active_entries(state) == []
    assert state["entries"][e["id"]]["versions"][0]["text"] == "会删的"


def test_replace_diff_keeps_matched_and_soft_deletes_missing(tmp_path):
    _bootstrap(tmp_path, "## 工具与环境\n- 甲\n- 乙\n")
    state = entries.replay(tmp_path)
    new_text = "## 工具与环境\n- 甲\n- 丙\n"
    result = view.diff_replace(state, new_text, SECTIONS)
    assert {e["text"] for e in result["kept"]} == {"甲"}
    assert {e["text"] for e in result["added"]} == {"丙"}
    assert {e["text"] for e in result["removed"]} == {"乙"}