# -*- coding: utf-8 -*-
"""置顶/非置顶拆分纯函数测试（不依赖 Qt 实例）"""

import importlib.util
import sys
from pathlib import Path

_MODULE = Path(__file__).resolve().parents[2] / "plugins" / "history-manager" / "ui" / "history_card.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("history_card_mod", str(_MODULE))
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("history_card_mod", mod)
    spec.loader.exec_module(mod)
    return mod


def _entry(i, sid, pinned=False, last_time="2026-09-12 00:00:00"):
    return (i, {"session_id": sid, "pinned": pinned, "last_time": last_time})


def test_split_moves_pinned_out_and_sorts_by_last_time():
    mod = _load_module()
    entries = [
        _entry(0, "a", pinned=True, last_time="2026-09-10 00:00:00"),
        _entry(1, "b"),
        _entry(2, "c", pinned=True, last_time="2026-09-11 00:00:00"),
    ]
    pinned, rest = mod.split_pinned_entries(entries)
    assert [s["session_id"] for _, s in pinned] == ["c", "a"]  # last_time 降序
    assert [s["session_id"] for _, s in rest] == ["b"]  # 保持相对顺序


def test_split_excludes_current_index():
    mod = _load_module()
    entries = [_entry(0, "a", pinned=True), _entry(1, "b", pinned=True)]
    pinned, rest = mod.split_pinned_entries(entries, exclude_index=0)
    assert [s["session_id"] for _, s in pinned] == ["b"]
    assert len(rest) == 1  # 被排除项落入 rest（当前会话区单独渲染）


def test_split_defaults_pinned_false():
    mod = _load_module()
    entries = [(0, {"session_id": "a", "last_time": "t"})]  # 无 pinned 字段
    pinned, rest = mod.split_pinned_entries(entries)
    assert pinned == [] and len(rest) == 1


def test_split_empty():
    mod = _load_module()
    assert mod.split_pinned_entries([]) == ([], [])
