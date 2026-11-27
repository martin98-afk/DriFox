# -*- coding: utf-8 -*-
"""test_memory_injection_priority.py — 注入装包的取舍策略。

覆盖：新条目优先（预算紧张时丢旧不丢新）、侧池 10% 宽限（全局有余额时不因
分区硬墙丢条目）、项目记忆整块取舍（整篇装得下不切块、超预算不切半块）。
"""
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_BASE = _ROOT / "plugins" / "assistant_hub" / "core" / "memory"

_entries_spec = importlib.util.spec_from_file_location("test_inj_entries", str(_BASE / "entries.py"))
entries = importlib.util.module_from_spec(_entries_spec)
sys.modules.setdefault("test_inj_entries", entries)
_entries_spec.loader.exec_module(entries)

_view_spec = importlib.util.spec_from_file_location("test_inj_view", str(_BASE / "view.py"))
view = importlib.util.module_from_spec(_view_spec)
sys.modules.setdefault("test_inj_view", view)
_view_spec.loader.exec_module(view)

SECTIONS = [
    {"name": "工程方法论", "hint": "方法", "tone": "rational", "detail": ""},
    {"name": "工具与环境", "hint": "工具", "tone": "rational", "detail": ""},
    {"name": "用户偏好", "hint": "偏好", "tone": "emotional", "detail": ""},
]


def _put(state, section, text, created_at):
    e = dict(entries.new_entry(section, text, origin="user"), created_at=created_at, updated_at=created_at)
    entries._apply_put(state, {"op": "put", "id": e["id"], "entry": e})
    return e


# ── 新条目优先 ────────────────────────────────────────────


def test_pack_keeps_newest_when_budget_tight():
    """预算只够一条时，装最新写入的那条，而不是最早的那条。"""
    state = entries.empty_state()
    old = _put(state, "工具与环境", "旧" * 60, "2026-01-01T00:00:00+08:00")
    new = _put(state, "工具与环境", "新" * 60, "2026-11-27T00:00:00+08:00")
    packed = view.pack_injection(state, SECTIONS, {"工具与环境": 4000, "用户偏好": 4000}, budget=80)
    assert f"- [{new['id']}]" in packed["text"]
    assert f"- [{old['id']}]" not in packed["text"]
    assert [o["id"] for o in packed["omitted"]] == [old["id"]]


def test_pinned_still_wins_over_newest():
    """置顶优先级高于新近：手动钉住的条目不能被新条目挤掉。"""
    state = entries.empty_state()
    pinned = _put(state, "工具与环境", "钉" * 60, "2026-01-01T00:00:00+08:00")
    entries._apply_pin(state, {"op": "pin", "id": pinned["id"], "pinned": True})
    _put(state, "工具与环境", "新" * 60, "2026-11-27T00:00:00+08:00")
    packed = view.pack_injection(state, SECTIONS, {"工具与环境": 4000, "用户偏好": 4000}, budget=80)
    assert f"- [{pinned['id']}]" in packed["text"]


# ── 侧池宽限 ──────────────────────────────────────────────


def test_pack_uses_global_budget_across_quota_slack():
    """理性侧超出平分额度一点点、全局仍有余额时，条目应被借额装进，不丢。"""
    state = entries.empty_state()
    # 工程方法论 100 字占满自己的 100 配额；工具与环境再加一条 50 字，
    # 侧池总配额 200，超出 50 属于 10% 宽限内（200*1.1=220）
    _put(state, "工程方法论", "甲" * 89, "2026-11-01T00:00:00+08:00")
    target = _put(state, "工具与环境", "乙" * 39, "2026-11-02T00:00:00+08:00")
    quotas = {"工程方法论": 100, "工具与环境": 100, "用户偏好": 400}
    packed = view.pack_injection(state, SECTIONS, quotas, budget=1000)
    assert f"- [{target['id']}]" in packed["text"]
    assert packed["omitted"] == []


def test_pack_still_drops_beyond_tolerance():
    """超出宽限（>10%）就照旧丢弃，宽限不是无限借额。"""
    state = entries.empty_state()
    _put(state, "工程方法论", "甲" * 89, "2026-11-01T00:00:00+08:00")
    huge = _put(state, "工具与环境", "乙" * 200, "2026-11-02T00:00:00+08:00")
    quotas = {"工程方法论": 100, "工具与环境": 100, "用户偏好": 400}
    packed = view.pack_injection(state, SECTIONS, quotas, budget=1000)
    assert f"- [{huge['id']}]" not in packed["text"]
    assert huge["id"] in [o["id"] for o in packed["omitted"]]


# ── 项目记忆整块取舍 ──────────────────────────────────────


def test_pack_project_returns_whole_text_when_it_fits():
    """整篇在预算内时原样返回：不因块切分的额外换行把最后一块挤掉。"""
    raw = "## 甲\n- 内容一\n\n## 乙\n- 内容二\n"
    packed = view.pack_project(raw, len(raw))
    assert packed["omitted"] == []
    assert "## 乙" in packed["text"]
    assert "（以下块" not in packed["text"]


def test_pack_project_drops_whole_block_never_half():
    """超预算时整块让位，被丢的块连标题都不出现，且列出块名。"""
    raw = "## 甲\n" + "甲" * 40 + "\n\n## 乙\n" + "乙" * 200 + "\n"
    packed = view.pack_project(raw, 100)
    assert "## 甲" in packed["text"]
    assert "乙" * 200 not in packed["text"]
    assert "## 乙" in packed["omitted"]
    assert "以下块因预算不足未注入" in packed["text"]


def test_pack_project_falls_back_to_first_block_when_nothing_fits():
    """一块都装不下时给首块的截断版并标注，避免项目记忆整段空掉。"""
    raw = "## 甲\n" + "甲" * 500 + "\n"
    packed = view.pack_project(raw, 200)
    assert packed["text"].strip()
    assert "已截断" in packed["text"]
    assert len(packed["text"]) < 260


def test_pack_project_tiny_budget_returns_empty():
    """预算小到连提示行都放不下：返回空文本，交给调用方提示。"""
    packed = view.pack_project("## 甲\n" + "甲" * 500, 30)
    assert packed["text"] == ""
    assert packed["omitted"]