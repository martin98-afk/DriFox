# -*- coding: utf-8 -*-
"""test_memory_store.py — 记忆存储层（双层结构 + 注入视图）回归。"""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "core" / "memory" / "store.py"

spec = importlib.util.spec_from_file_location("test_memory_store_mod", str(_MODULE))
m = importlib.util.module_from_spec(spec)
sys.modules.setdefault("test_memory_store_mod", m)
spec.loader.exec_module(m)


def test_global_memory_roundtrip(tmp_path):
    aid_dir = tmp_path / "hub" / "build"
    aid_dir.mkdir(parents=True)
    assert m.read_global_memory(aid_dir) == ""
    m.write_global_memory(aid_dir, "用户偏好简洁回复")
    assert "偏好简洁" in m.read_global_memory(aid_dir)
    assert m.global_memory_path(aid_dir) == aid_dir / "MEMORY.md"


def test_project_memory_roundtrip(tmp_path):
    wd = str(tmp_path / "proj")
    m.write_project_memory(wd, "build", "# 项目约定\n\n- 提交用中文")
    text = m.read_project_memory(wd, "build")
    assert "提交用中文" in text
    assert m.project_memory_path(wd, "build") == Path(wd) / ".drifox" / "memory" / "build" / "MEMORY.md"


def test_append_daily_no_header_entry_stream(tmp_path):
    wd = str(tmp_path / "proj")
    r1 = m.append_daily(wd, "build", "- 修复了 X 崩溃")
    assert r1["ok"]
    r2 = m.append_daily(wd, "build", "- 新增了 Y 功能")
    assert r2["ok"]
    body = m.read_daily(wd, "build")
    # 文件名即日期：正文纯条目流，无日期标题；条目间空行分隔
    assert not body.lstrip().startswith("#")
    assert "修复了 X 崩溃" in body and "新增了 Y 功能" in body
    assert body.index("X 崩溃") < body.index("Y 功能")
    assert "\n\n" in body  # 条目间有空行


def test_append_daily_empty_rejected(tmp_path):
    r = m.append_daily(str(tmp_path), "build", "   ")
    assert not r["ok"] and r["error"] == "empty"


def test_daily_path_rejects_non_date(tmp_path):
    """date 参数仅接受 YYYY-MM-DD：memory_write 是 safe 工具，须防路径穿越。"""
    for bad in ["../evil", "..\\evil", "2026-10-1a", "x/y", "  "]:
        wd = str(tmp_path / "proj")
        m.append_daily(wd, "build", "- 正常写入", "2026-10-13")
        try:
            m.read_daily(wd, "build", bad)
            raised = False
        except ValueError:
            raised = True
        assert raised, f"date={bad!r} 应拒绝"
    # 空串 = 缺省今天，合法
    assert m.read_daily(str(tmp_path / "proj2"), "build", "") == ""


def test_list_daily_filters_non_date_files(tmp_path):
    """用户放进 memory 目录的杂项 .md 不进日志索引。"""
    wd = str(tmp_path / "proj")
    d = Path(wd) / ".drifox" / "memory" / "build"
    d.mkdir(parents=True)
    (d / "2026-10-01.md").write_text("- 日志", encoding="utf-8")
    (d / "notes.md").write_text("杂项", encoding="utf-8")
    (d / "MEMORY.md").write_text("笔记", encoding="utf-8")
    items = m.list_daily(wd, "build", limit=10)
    assert [it["date"] for it in items] == ["2026-10-01"]


def test_append_daily_no_workdir_rejected():
    r = m.append_daily("", "build", "- x")
    assert not r["ok"] and r["error"] == "no_workdir"


def test_list_daily_newest_first_and_limit(tmp_path):
    wd = str(tmp_path / "proj")
    m.append_daily(wd, "build", "- 一号", "2026-10-01")
    m.append_daily(wd, "build", "- 二号", "2026-10-03")
    m.append_daily(wd, "build", "- 三号", "2026-10-02")
    items = m.list_daily(wd, "build", limit=10)
    assert [it["date"] for it in items] == ["2026-10-03", "2026-10-02", "2026-10-01"]
    items2 = m.list_daily(wd, "build", limit=2)
    assert len(items2) == 2 and items2[0]["date"] == "2026-10-03"
    # MEMORY.md 不算日志
    m.write_project_memory(wd, "build", "note")
    assert all(it["date"] != "MEMORY" for it in m.list_daily(wd, "build", limit=10))


def test_stale_daily_threshold(tmp_path):
    wd = str(tmp_path / "proj")
    m.append_daily(wd, "build", "- 旧", "2020-01-01")
    m.append_daily(wd, "build", "- 新", "2999-12-31")
    stale = m.stale_daily(wd, "build", days=30)
    assert "2020-01-01" in stale and "2999-12-31" not in stale


def test_clear_memory_removes_both_layers(tmp_path):
    aid_dir = tmp_path / "hub" / "build"
    aid_dir.mkdir(parents=True)
    wd = str(tmp_path / "proj")
    m.write_global_memory(aid_dir, "全局")
    m.write_project_memory(wd, "build", "项目")
    m.append_daily(wd, "build", "- 日志")
    m.clear_memory(aid_dir, wd)
    assert m.read_global_memory(aid_dir) == ""
    assert m.read_project_memory(wd, "build") == ""
    assert m.list_daily(wd, "build") == []


def test_daily_index_text_lists_entries(tmp_path):
    wd = str(tmp_path / "proj")
    m.append_daily(wd, "build", "- 一条", "2026-10-10")
    text = m.daily_index_text(wd, "build")
    assert "2026-10-10" in text and "memory_read" in text


def test_no_workdir_reads_return_empty():
    assert m.read_project_memory("", "build") == ""
    assert m.read_daily("", "build") == ""
    assert m.list_daily("", "build") == []
    assert m.daily_index_text("", "build") == ""
