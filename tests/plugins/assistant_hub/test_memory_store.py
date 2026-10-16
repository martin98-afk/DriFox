# -*- coding: utf-8 -*-
"""test_memory_store.py — 记忆存储层（双层结构 + 注入视图）回归。"""

import importlib.util
import json
import re
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


def test_append_daily_prefix_and_level(tmp_path):
    """程序自动打 loguru 风格前缀：时刻 | 等级 | 内容；非法 level 回落 INFO。"""
    wd = str(tmp_path / "proj")
    m.append_daily(wd, "build", "- 修了 X")
    m.append_daily(wd, "build", "- 坑：Y 会崩", level="WARNING")
    m.append_daily(wd, "build", "- 乱写的等级", level="bogus")
    body = m.read_daily(wd, "build")
    assert " | INFO | - 修了 X" in body
    assert " | WARNING | - 坑：Y 会崩" in body
    assert " | INFO | - 乱写的等级" in body
    # 时刻格式：YYYY-MM-DD HH:MM:SS
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| INFO \| ", body)


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
    assert "2026-10-10" in text and "date=YYYY-MM-DD" in text


def test_no_workdir_reads_return_empty():
    assert m.read_project_memory("", "build") == ""
    assert m.read_daily("", "build") == ""
    assert m.list_daily("", "build") == []
    assert m.daily_index_text("", "build") == ""


# ── 蒸馏标记 / 门禁 / 分页 ────────────────────────────────


def test_parse_daily_stem():
    assert m.parse_daily_stem("2026-10-15") == ("2026-10-15", False)
    assert m.parse_daily_stem("2026-10-15.distilled") == ("2026-10-15", True)
    assert m.parse_daily_stem("notes") is None
    assert m.parse_daily_stem("MEMORY") is None
    assert m.parse_daily_stem("2026-10-15.bak") is None


def test_mark_distilled_roundtrip(tmp_path):
    wd = str(tmp_path / "proj")
    m.append_daily(wd, "build", "- 一条", "2026-10-15")
    r = m.mark_distilled(wd, "build", "2026-10-15")
    assert r["ok"]
    d = Path(wd) / ".drifox" / "memory" / "build"
    assert (d / "2026-10-15.md").exists() is False
    assert (d / "2026-10-15.distilled.md").exists()
    # 错误分支
    assert m.mark_distilled(wd, "build", "2026-10-15")["error"] == "already"
    assert m.mark_distilled(wd, "build", "2026-10-14")["error"] == "not_found"
    assert m.mark_distilled(wd, "build", "../evil")["error"] == "bad_date"
    assert m.mark_distilled("", "build", "2026-10-15")["error"] == "no_workdir"


def test_list_daily_reports_distilled(tmp_path):
    wd = str(tmp_path / "proj")
    m.append_daily(wd, "build", "- 甲", "2026-10-14")
    m.append_daily(wd, "build", "- 乙", "2026-10-15")
    m.mark_distilled(wd, "build", "2026-10-14")
    items = m.list_daily(wd, "build", limit=10)
    by_date = {it["date"]: it for it in items}
    assert by_date["2026-10-14"]["distilled"] is True
    assert by_date["2026-10-15"]["distilled"] is False


def test_read_daily_falls_back_to_distilled(tmp_path):
    wd = str(tmp_path / "proj")
    m.append_daily(wd, "build", "- 蒸馏后仍可查", "2026-10-14")
    m.mark_distilled(wd, "build", "2026-10-14")
    assert "蒸馏后仍可查" in m.read_daily(wd, "build", "2026-10-14")


def test_read_daily_paged(tmp_path):
    wd = str(tmp_path / "proj")
    for i in range(25):
        m.append_daily(wd, "build", f"- 条目{i}", "2026-10-14")
    r = m.read_daily_paged(wd, "build", "2026-10-14", page=1)
    assert r["ok"] and r["pages"] == 2 and r["total"] == 25
    assert "条目0" in r["content"] and "条目19" in r["content"] and "条目20" not in r["content"]
    r2 = m.read_daily_paged(wd, "build", "2026-10-14", page=2)
    assert "条目24" in r2["content"] and "条目5" not in r2["content"]
    # 越界 clamp 到最后一页；单条不分页
    assert m.read_daily_paged(wd, "build", "2026-10-14", page=99)["page"] == 2
    single = m.read_daily_paged(wd, "build", "2026-10-15", page=1)
    assert not single["ok"] and single["error"] == "not_found"
    m.append_daily(wd, "build", "- 唯一", "2026-10-15")
    r3 = m.read_daily_paged(wd, "build", "2026-10-15")
    assert r3["pages"] == 1 and r3["total"] == 1


def test_read_daily_paged_marks_distilled(tmp_path):
    wd = str(tmp_path / "proj")
    m.append_daily(wd, "build", "- 甲", "2026-10-14")
    m.mark_distilled(wd, "build", "2026-10-14")
    r = m.read_daily_paged(wd, "build", "2026-10-14")
    assert r["ok"] and r["distilled"] is True


def test_undistilled_before_and_daily_exists(tmp_path):
    wd = str(tmp_path / "proj")
    m.append_daily(wd, "build", "- 旧", "2026-10-13")
    m.append_daily(wd, "build", "- 中", "2026-10-14")
    m.append_daily(wd, "build", "- 昨", "2026-10-15")
    m.mark_distilled(wd, "build", "2026-10-13")
    assert m.undistilled_before(wd, "build", "2026-10-16") == ["2026-10-14", "2026-10-15"]
    assert m.undistilled_before(wd, "build", "2026-10-15") == ["2026-10-14"]
    assert m.undistilled_before(wd, "build") == m.undistilled_before(wd, "build", m.today())
    # 当天日志不算「之前」
    m.append_daily(wd, "build", "- 今", "2026-10-16")
    assert m.undistilled_before(wd, "build", "2026-10-16") == ["2026-10-14", "2026-10-15"]
    assert m.daily_exists(wd, "build", "2026-10-16")
    assert not m.daily_exists(wd, "build", "2026-10-12")


def test_stale_daily_excludes_distilled(tmp_path):
    wd = str(tmp_path / "proj")
    m.append_daily(wd, "build", "- 旧甲", "2020-01-01")
    m.append_daily(wd, "build", "- 旧乙", "2020-02-01")
    m.mark_distilled(wd, "build", "2020-01-01")
    assert m.stale_daily(wd, "build", days=30) == ["2020-02-01"]


def test_distilled_index_tag(tmp_path):
    wd = str(tmp_path / "proj")
    m.append_daily(wd, "build", "- 甲", "2026-10-14")
    m.mark_distilled(wd, "build", "2026-10-14")
    text = m.daily_index_text(wd, "build")
    assert "已蒸馏" in text


# ── 快照（覆盖写前的历史版本）──────────────────────────


def _mk_aid(tmp_path):
    aid_dir = tmp_path / "hub" / "build"
    aid_dir.mkdir(parents=True)
    return aid_dir


def test_snapshot_roundtrip_and_restore_backup(tmp_path):
    aid_dir = _mk_aid(tmp_path)
    g = aid_dir / "MEMORY.md"
    g.write_text("v1 内容", encoding="utf-8")
    r1 = m.snapshot_memory(aid_dir, "global", g)
    assert r1["ok"] and r1["file"]
    g.write_text("v2 内容", encoding="utf-8")
    r2 = m.snapshot_memory(aid_dir, "global", g)
    snaps = m.list_snapshots(aid_dir, "global")
    assert len(snaps) == 2
    assert snaps[0]["file"] == r2["file"]  # 时间倒序，最新在前
    assert m.read_snapshot(aid_dir, "global", r2["file"]) == "v2 内容"
    # 恢复 v1：恢复前当前版 v2 自动入快照
    rr = m.restore_snapshot(aid_dir, "global", r1["file"], g)
    assert rr["ok"] and rr["backup"]
    assert m.read_text(g) == "v1 内容"
    assert len(m.list_snapshots(aid_dir, "global")) == 3


def test_snapshot_no_source_rejected(tmp_path):
    aid_dir = _mk_aid(tmp_path)
    assert m.snapshot_memory(aid_dir, "global", aid_dir / "不存在.md")["ok"] is False
    empty = aid_dir / "空.md"
    empty.write_text("   ", encoding="utf-8")
    assert m.snapshot_memory(aid_dir, "global", empty)["ok"] is False


def test_snapshot_prune_keeps_ten(tmp_path):
    aid_dir = _mk_aid(tmp_path)
    g = aid_dir / "MEMORY.md"
    g.write_text("x", encoding="utf-8")
    sdir = m.snapshot_dir(aid_dir, "global")
    sdir.mkdir(parents=True)
    for d in range(1, 13):  # 手工造 12 份历史快照（名字即时间序）
        (sdir / f"20261001-0000{d:02d}.md").write_text(f"s{d}", encoding="utf-8")
    m.snapshot_memory(aid_dir, "global", g)
    snaps = m.list_snapshots(aid_dir, "global")
    assert len(snaps) == 10
    names = [s["file"] for s in snaps]
    assert "20261001-000001.md" not in names  # 最旧被裁
    assert "20261001-000012.md" in names


def test_snapshot_project_origin_json(tmp_path):
    aid_dir = _mk_aid(tmp_path)
    wd = str(tmp_path / "proj")
    m.write_project_memory(wd, "build", "项目记忆")
    r = m.snapshot_memory(aid_dir, "project", m.project_memory_path(wd, "build"), wd)
    assert r["ok"]
    origin = m.snapshot_dir(aid_dir, "project", wd) / "origin.json"
    assert origin.exists() and json.loads(origin.read_text(encoding="utf-8"))["workdir"] == wd
    # origin.json 不计入快照列表，也不被裁剪
    assert all(s["file"] != "origin.json" for s in m.list_snapshots(aid_dir, "project", wd))


def test_snapshot_bad_name_and_kind_rejected(tmp_path):
    aid_dir = _mk_aid(tmp_path)
    for bad in ["../evil.md", "x.md", ""]:
        try:
            m.read_snapshot(aid_dir, "global", bad)
            raised = False
        except ValueError:
            raised = True
        assert raised, f"name={bad!r} 应拒绝"
    try:
        m.snapshot_dir(aid_dir, "bogus")
        raised = False
    except ValueError:
        raised = True
    assert raised


# ── 工作目录状态与漂移 ───────────────────────────────────


def test_workdir_drift_detect_and_migrate(tmp_path):
    aid_dir = _mk_aid(tmp_path)
    old_wd = str(tmp_path / "old")
    new_wd = str(tmp_path / "new")
    m.write_project_memory(old_wd, "build", "旧项目记忆")
    # 无 last_workdir 记录 → 不报漂移
    assert m.detect_workdir_drift(aid_dir, new_wd)["drifted"] is False
    m.touch_workdir(aid_dir, old_wd)
    d = m.detect_workdir_drift(aid_dir, new_wd)
    assert d["drifted"] is True and d["old_workdir"] == old_wd and d["old_chars"] > 0
    # 迁移：MEMORY.md + 日志一起搬到新目录（旧目录文件保留）
    m.append_daily(old_wd, "build", "- 旧日志条目", date="2026-10-10")
    r = m.migrate_project_memory(aid_dir, old_wd, new_wd)
    assert r["ok"] and "MEMORY.md" in r["moved"]
    assert "旧项目记忆" in m.read_project_memory(new_wd, "build")
    assert "旧日志条目" in m.read_daily(new_wd, "build", "2026-10-10")
    # 迁移后 touch 新目录 → 不再报漂移
    assert m.detect_workdir_drift(aid_dir, new_wd)["drifted"] is False


def test_drift_dismiss_stops_reporting(tmp_path):
    aid_dir = _mk_aid(tmp_path)
    old_wd = str(tmp_path / "old")
    new_wd = str(tmp_path / "new")
    m.touch_workdir(aid_dir, old_wd)
    m.write_project_memory(old_wd, "build", "旧记忆")
    assert m.detect_workdir_drift(aid_dir, new_wd)["drifted"] is True
    m.dismiss_drift(aid_dir, old_wd)
    assert m.detect_workdir_drift(aid_dir, new_wd)["drifted"] is False


def test_migrate_rejects_non_empty_target(tmp_path):
    aid_dir = _mk_aid(tmp_path)
    old_wd = str(tmp_path / "old")
    new_wd = str(tmp_path / "new")
    m.write_project_memory(old_wd, "build", "旧")
    m.write_project_memory(new_wd, "build", "新")
    r = m.migrate_project_memory(aid_dir, old_wd, new_wd)
    assert not r["ok"] and r["error"] == "target_not_empty"


def test_drift_current_memory_nonempty_is_normal(tmp_path):
    aid_dir = _mk_aid(tmp_path)
    wd = str(tmp_path / "cur")
    m.touch_workdir(aid_dir, str(tmp_path / "elsewhere"))
    m.write_project_memory(wd, "build", "当前有记忆")
    assert m.detect_workdir_drift(aid_dir, wd)["drifted"] is False
