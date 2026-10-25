# -*- coding: utf-8 -*-
"""test_memory_tools.py — memory_read / memory_write 工具回归（假 manager 注入）。

覆盖：基本读写、蒸馏门禁（新日首写拦截/放行）、打标（mark_distilled）、
单日条目分页、索引 limit。
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "tools" / "memory_tools.py"

spec = importlib.util.spec_from_file_location("test_memory_tools_mod", str(_MODULE))
m = importlib.util.module_from_spec(spec)
sys.modules.setdefault("test_memory_tools_mod", m)
spec.loader.exec_module(m)

# store.py 仅取收缩阈值常量，保证假 manager 与真实现判定一致
_STORE_MOD = _ROOT / "plugins" / "assistant_hub" / "core" / "memory" / "store.py"
_spec2 = importlib.util.spec_from_file_location("test_memory_tools_store", str(_STORE_MOD))
store = importlib.util.module_from_spec(_spec2)
sys.modules.setdefault("test_memory_tools_store", store)
_spec2.loader.exec_module(store)


class _Assistant:
    memory_enabled = True


class _Mgr:
    """假 manager：内存态后端 + 固定归属。"""

    def __init__(self, tmp_path):
        self._g = {}
        self._p = {}
        self._d = {}  # (aid, wd, date) -> [entries]
        self._distilled = set()  # (aid, wd, date)
        self.gate_text = ""  # distill_gate_block 返回值（门禁测试可控）
        self.gate_calls = 0
        self.last_level = ""  # 最近一次 append_daily 的 level（透传测试）
        self.last_summary = ""
        self.last_query: Optional[Dict[str, Any]] = None  # 最近一次 query_daily 的参数（查询模式透传测试）
        self.snapshots = []  # (aid, kind) — 写前快照调用记录
        self.sections: list = []  # 全局记忆分区定义（空 = 自由格式）
        self._tmp = tmp_path

    def _today(self):
        return datetime.now().strftime("%Y-%m-%d")

    def has(self, aid):
        return aid == "build"

    def active_id(self):
        return "build"

    def resolve_session_aid(self, sid):
        return "build"

    def get(self, aid):
        return _Assistant()

    def reload_from_disk(self):
        pass

    # 记忆门面（模拟 manager 转发 store）
    def read_global_memory(self, aid):
        return self._g.get(aid, "")

    def write_global_memory(self, aid, content):
        self._g[aid] = content
        return True

    def read_project_memory(self, aid, wd):
        return self._p.get((aid, wd), "")

    def write_project_memory(self, aid, wd, content):
        self._p[(aid, wd)] = content
        return True

    def append_daily(self, aid, wd, text, date="", level="INFO", summary=""):
        date = date or self._today()
        self.last_level = level
        self.last_summary = summary
        self._d.setdefault((aid, wd, date), []).append(text)
        return {"ok": True, "date": date, "chars": sum(len(x) for x in self._d[(aid, wd, date)])}

    def list_daily(self, aid, wd, limit=7):
        items = [
            {
                "date": k[2],
                "chars": sum(len(x) for x in v),
                "distilled": k in self._distilled,
            }
            for k, v in self._d.items()
            if k[0] == aid and k[1] == wd
        ]
        items.sort(key=lambda it: it["date"], reverse=True)
        return items[:limit]

    def read_daily(self, aid, wd, date=""):
        date = date or self._today()
        return "\n\n".join(self._d.get((aid, wd, date), []))

    def read_daily_paged(self, aid, wd, date="", page=1, per_page=20):
        date = date or self._today()
        entries = self._d.get((aid, wd, date), [])
        if not entries:
            return {"ok": False, "error": "not_found"}
        total = len(entries)
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(max(int(page or 1), 1), pages)
        return {
            "ok": True,
            "content": "\n\n".join(entries[(page - 1) * per_page : page * per_page]),
            "page": page,
            "pages": pages,
            "total": total,
            "distilled": (aid, wd, date) in self._distilled,
        }

    def daily_exists(self, aid, wd, date=""):
        date = date or self._today()
        return bool(self._d.get((aid, wd, date)))

    def undistilled_daily_before(self, aid, wd, before=""):
        b = before or self._today()
        return sorted(k[2] for k in self._d if k[0] == aid and k[1] == wd and k[2] < b and k not in self._distilled)

    def mark_daily_distilled(self, aid, wd, date):
        key = (aid, wd, date)
        if not any(k[0] == aid and k[1] == wd and k[2] == date for k in self._d):
            return {"ok": False, "error": "not_found"}
        if key in self._distilled:
            return {"ok": False, "error": "already"}
        self._distilled.add(key)
        return {"ok": True, "date": date, "file": ""}

    def distill_gate_block(self, aid, wd):
        self.gate_calls += 1
        return self.gate_text

    def memory_max_chars(self, aid=""):
        return {"global": 4000, "project": 3000}

    # 分区门面（schema 空 = 自由格式，与真 manager 一致）
    def memory_sections(self, aid):
        return self.sections

    def validate_global_memory(self, aid, content):
        return store.validate_global_sections(content, self.memory_sections(aid))

    def snapshot_memory(self, aid, kind, workdir=""):
        self.snapshots.append((aid, kind))
        return {"ok": True, "file": "snap.md"}

    def memory_paths(self, aid, wd=""):
        out = {"global": str(self._tmp / "MEMORY.md")}
        if wd:
            out["project"] = str(self._tmp / "proj" / "MEMORY.md")
        return out

    # ── 乐观锁 / append（模拟 store 语义：内容变化 → 指纹变化）──
    @staticmethod
    def _fp(text):
        return f"f{len(text)}"

    def memory_fingerprint(self, aid, kind, workdir=""):
        src = self._g.get(aid, "") if kind == "global" else self._p.get((aid, workdir), "")
        return self._fp(src)

    def append_memory(self, aid, kind, text, workdir="", section=""):
        if kind == "global":
            cur = self._g.get(aid, "")
            if section:
                self._g[aid] = store.insert_into_section(cur, section, text.strip())
                return {"ok": True, "chars": len(self._g[aid])}
            self._g[aid] = (cur.rstrip() + "\n" + text.strip()) if cur.strip() else text.strip()
            return {"ok": True, "chars": len(self._g[aid])}
        if kind == "project" and workdir:
            key = (aid, workdir)
            cur = self._p.get(key, "")
            self._p[key] = (cur.rstrip() + "\n" + text.strip()) if cur.strip() else text.strip()
            return {"ok": True, "chars": len(self._p[key])}
        return {"ok": False, "error": "bad_kind"}

    def write_memory_guarded(self, aid, kind, content, base="", consolidate=False, snapshot=None, workdir=""):
        key = aid if kind == "global" else (aid, workdir)
        bag = self._g if kind == "global" else self._p
        cur = bag.get(key, "")
        cur_chars = len(cur.strip())
        fp = self._fp(cur)
        if base and base != fp:
            return {"ok": False, "error": "conflict", "cur_base": fp}
        new_chars = len(content.strip())
        if (
            not consolidate
            and cur_chars >= store.SHRINK_CHECK_MIN_CHARS
            and new_chars < cur_chars * store.SHRINK_RATIO
        ):
            return {"ok": False, "error": "shrink", "old_chars": cur_chars, "new_chars": new_chars}
        if snapshot is not None:
            try:
                snapshot()
            except Exception:
                pass
        bag[key] = content
        return {"ok": True, "chars": new_chars}

    def query_daily(
        self,
        aid,
        wd,
        from_date="",
        to_date="",
        levels=None,
        search="",
        summary_only=False,
        page=1,
        per_page=20,
    ):
        """打桩：记录参数并返回固定命中（透传与分支测试）。"""
        self.last_query = {
            "aid": aid,
            "wd": wd,
            "from_date": from_date,
            "to_date": to_date,
            "levels": list(levels or []),
            "search": search,
            "summary_only": summary_only,
            "page": page,
        }
        return {
            "ok": True,
            "content": "2026-10-16 10:00:00 | INFO | 桩条目 | 正文"
            if (levels or search or summary_only or from_date or to_date)
            else "",
            "page": 1,
            "pages": 1,
            "total": 1,
            "matched": 1 if (levels or search or summary_only or from_date or to_date) else 0,
            "days": ["2026-10-16"] if (levels or search or summary_only or from_date or to_date) else [],
            "search_mode": ("regex" if search else ""),
        }


def _patch(monkeypatch, tmp_path):
    mgr = _Mgr(tmp_path)
    monkeypatch.setattr(m, "_get_manager", lambda: mgr)
    return mgr


def _ctx(wd=""):
    return {"session_id": "s1", "workdir": wd}


def test_write_daily_then_read(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    today = mgr._today()
    r = m._write_impl(_ctx("/proj"), scope="daily", content="- 完成了记忆重构")
    assert r.success and "追加" in r.content
    # 不带 date：返回近期日志索引
    r2 = m._read_impl(_ctx("/proj"), scope="daily")
    assert today in r2.content
    # 带 date 精确读全文
    r3 = m._read_impl(_ctx("/proj"), scope="daily", date=today)
    assert "完成了记忆重构" in r3.content


def test_write_global_and_read(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    r = m._write_impl(_ctx("/proj"), scope="global", content="用户要求中文回复")
    assert r.success
    r2 = m._read_impl(_ctx("/proj"), scope="global")
    assert "中文回复" in r2.content


def test_write_project_requires_workdir(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    r = m._write_impl(_ctx(""), scope="project", content="x")
    assert r.success and "工作目录" in r.content
    r2 = m._read_impl(_ctx(""), scope="project")
    assert "工作目录" in r2.content


def test_write_scope_invalid(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    r = m._write_impl(_ctx("/proj"), scope="bogus", content="x")
    assert not r.success


def test_write_empty_content_rejected(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    r = m._write_impl(_ctx("/proj"), scope="daily", content="   ")
    assert not r.success


def test_write_daily_too_long_rejected(monkeypatch, tmp_path):
    """store 侧 too_long 需转成可执行的压缩指令，而非裸错误码。"""
    mgr = _patch(monkeypatch, tmp_path)
    mgr.append_daily = lambda *a, **k: {"ok": False, "error": "too_long", "chars": 900, "limit": 400}
    r = m._write_impl(_ctx("/proj"), scope="daily", content="x" * 900)
    assert not r.success
    assert "900/400" in r.error and "日志是索引不是报告" in r.error


def test_write_paused_when_memory_disabled(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)

    class _Off:
        memory_enabled = False

    mgr.get = lambda aid: _Off()
    r = m._write_impl(_ctx("/proj"), scope="daily", content="- x")
    assert r.success and "暂停" in r.content
    # 读仍可用
    r2 = m._read_impl(_ctx("/proj"), scope="global")
    assert r2.success


def test_read_overview_no_scope(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    # 直塞假数据造历史日志（写路径恒落今日，不能再用 date 参数造数）
    mgr._d[(mgr.active_id(), "/proj", "2026-10-13")] = ["- x"]
    r = m._read_impl(_ctx("/proj"))
    assert "全局记忆" in r.content and "项目记忆" in r.content and "2026-10-13" in r.content


def test_manager_unavailable(monkeypatch):
    monkeypatch.setattr(m, "_get_manager", lambda: None)
    r = m._read_impl(_ctx("/proj"))
    assert not r.success
    r2 = m._write_impl(_ctx("/proj"), scope="daily", content="x")
    assert not r2.success


# ── 新日蒸馏门禁 ──────────────────────────────────────────


def test_gate_blocks_first_write_of_day(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    mgr.gate_text = "【蒸馏指令】先蒸馏昨日日志"
    r = m._write_impl(_ctx("/proj"), scope="daily", content="- 今天第一条")
    assert not r.success
    assert "蒸馏" in (r.error or "")
    assert mgr.gate_calls == 1


def test_gate_passes_when_all_distilled(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    mgr.gate_text = ""  # 无未蒸馏旧日志
    r = m._write_impl(_ctx("/proj"), scope="daily", content="- 今天第一条")
    assert r.success and "追加" in r.content


def test_gate_passes_once_day_started(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    mgr.gate_text = "【蒸馏指令】"
    today = mgr._today()
    # 模型传的 date 在写路径无效：恒写当日 → 补录同样触发门禁、落在今天文件
    r0 = m._write_impl(_ctx("/proj"), scope="daily", content="- 补录", date="2020-01-01")
    assert not r0.success and mgr.gate_calls == 1
    assert "2020-01-01" not in {k[2] for k in mgr._d}
    # 当天已写过一条后，第二条不再拦截
    mgr._d[(mgr.active_id(), "/proj", today)] = ["- 首条"]
    r = m._write_impl(_ctx("/proj"), scope="daily", content="- 第二条")
    assert r.success and mgr.gate_calls == 1
def test_write_daily_ignores_model_date(monkeypatch, tmp_path):
    """回归：追加恒写系统当日日志，模型传的历史 date 无效（含门禁放行场景）"""
    mgr = _patch(monkeypatch, tmp_path)
    mgr.gate_text = ""  # 无未蒸馏旧日志，门禁放行
    today = mgr._today()
    r = m._write_impl(_ctx("/proj"), scope="daily", content="- 一条", date="2020-01-01")
    assert r.success and today in r.content
    keys = [k[2] for k in mgr._d]
    assert keys == [today], f"应落在当日文件 {today}，实际 {keys}"


def test_gate_query_failure_does_not_block(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)

    def _boom(aid, wd):
        raise RuntimeError("boom")

    mgr.distill_gate_block = _boom
    r = m._write_impl(_ctx("/proj"), scope="daily", content="- 今天第一条")
    assert r.success  # 门禁异常放行，不阻断写入


# ── 打标 mark_distilled ──────────────────────────────────


def test_mark_distilled_flow(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    aid = mgr.active_id()
    # 无 content 且非打标 → 拒绝
    r = m._write_impl(_ctx("/proj"), scope="daily", content="   ")
    assert not r.success
    # 无未蒸馏日志：打标为空操作
    r0 = m._write_impl(_ctx("/proj"), scope="daily", mark_distilled=True)
    assert r0.success and "没有未打标" in r0.content
    # 旧日志 → 全量打标；打标后不再出现在未蒸馏清单（全量语义=今天之前，与门禁对齐）
    mgr._d[(aid, "/proj", "2026-10-20")] = ["- 旧条目"]
    r2 = m._write_impl(_ctx("/proj"), scope="daily", mark_distilled=True)
    assert r2.success and "打蒸馏标" in r2.content and "2026-10-20" in r2.content
    assert (aid, "/proj", "2026-10-20") in mgr._distilled
    r3 = m._write_impl(_ctx("/proj"), scope="daily", mark_distilled=True)
    assert r3.success and "没有未打标" in r3.content

def test_mark_distilled_reports_merged(monkeypatch, tmp_path):
    """store 收敛残留条目时（merged>0），返回消息须告知模型"""
    mgr = _patch(monkeypatch, tmp_path)
    mgr._d[(mgr.active_id(), "/proj", "2026-10-20")] = ["- 甲"]
    def _fake_mark(aid, wd, date):
        return {"ok": True, "date": date, "file": "x", "merged": 2}
    mgr.mark_daily_distilled = _fake_mark
    r = m._write_impl(_ctx("/proj"), scope="daily", mark_distilled=True)
    assert r.success and "2" in r.content and "并入" in r.content

def test_mark_distilled_partial_failure(monkeypatch, tmp_path):
    """多日打标部分失败：成功的照常报告，失败列在尾部"""
    mgr = _patch(monkeypatch, tmp_path)
    aid = mgr.active_id()
    mgr._d[(aid, "/proj", "2026-10-20")] = ["- a"]
    mgr._d[(aid, "/proj", "2026-10-21")] = ["- b"]
    def _flaky(aid, wd, date):
        if date == "2026-10-21":
            return {"ok": False, "error": "boom"}
        return {"ok": True, "date": date, "file": "x", "merged": 0}
    mgr.mark_daily_distilled = _flaky
    r = m._write_impl(_ctx("/proj"), scope="daily", mark_distilled=True)
    assert r.success and "2026-10-20" in r.content and "boom" in r.content


# ── 读取分页 / 索引 limit ─────────────────────────────────


def test_read_daily_paged_tool(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    today = mgr._today()
    for i in range(25):
        m._write_impl(_ctx("/proj"), scope="daily", content=f"- 条目{i}")
    r = m._read_impl(_ctx("/proj"), scope="daily", date=today)
    assert f"{today}（第 1/2 页，共 25 条）" in r.content
    assert "条目0" in r.content and "条目20" not in r.content
    assert "page=2" in r.content
    r2 = m._read_impl(_ctx("/proj"), scope="daily", date=today, page=2)
    assert "第 2/2 页" in r2.content and "条目24" in r2.content
    assert "翻页" not in r2.content  # 末页不再提示翻页
    # 无日志
    r3 = m._read_impl(_ctx("/proj"), scope="daily", date="2020-01-01")
    assert r3.success and "无日志" in r3.content


def test_write_daily_level_passthrough(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    m._write_impl(_ctx("/proj"), scope="daily", content="- 甲")
    assert mgr.last_level == "INFO"
    m._write_impl(_ctx("/proj"), scope="daily", content="- 乙", level="WARNING", summary="摘要乙")
    assert mgr.last_level == "WARNING"
    assert mgr.last_summary == "摘要乙"


def test_read_daily_index_limit(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    # 直塞三日假数据（写路径恒落今日，造历史数据走 _d）
    aid = mgr.active_id()
    for d in ["2026-10-01", "2026-10-02", "2026-10-03"]:
        mgr._d[(aid, "/proj", d)] = [f"- {d}"]
    r = m._read_impl(_ctx("/proj"), scope="daily", limit=2)
    assert "2026-10-03" in r.content and "2026-10-02" in r.content and "2026-10-01" not in r.content


# ── 查询模式（等级 / 日期范围 / 搜索 / 仅摘要）──────────────


def test_read_daily_query_level(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    r = m._read_impl(_ctx("/proj"), scope="daily", level="warning,error")
    assert r.success and "等级 WARNING,ERROR" in r.content and "命中 1/1" in r.content
    assert "桩条目" in r.content
    q = mgr.last_query
    assert q is not None and q["levels"] == ["WARNING", "ERROR"] and q["from_date"] == "" and q["to_date"] == ""
    # 不带过滤参数不走查询（近期索引）
    mgr.last_query = None
    mgr._d[(mgr.active_id(), "/proj", "2026-10-01")] = ["- 有条目"]
    r2 = m._read_impl(_ctx("/proj"), scope="daily")
    assert mgr.last_query is None and "近期工作日志" in r2.content and "2026-10-01" in r2.content


def test_read_daily_query_range_and_summary_only(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    r = m._read_impl(_ctx("/proj"), scope="daily", from_date="2026-10-10", to_date="2026-10-16", summary_only=True)
    assert r.success and "仅摘要" in r.content and "2026-10-10~2026-10-16" in r.content
    q = mgr.last_query
    assert q is not None and q["from_date"] == "2026-10-10" and q["to_date"] == "2026-10-16" and q["summary_only"] is True


def test_read_daily_query_single_date_with_filter(monkeypatch, tmp_path):
    """date + 过滤参数 → 单日过滤查询（from=to=date）。"""
    mgr = _patch(monkeypatch, tmp_path)
    r = m._read_impl(_ctx("/proj"), scope="daily", date="2026-10-16", search="坑")
    assert r.success and "搜索「坑」（正则）" in r.content
    q = mgr.last_query
    assert q is not None and q["from_date"] == "2026-10-16" and q["to_date"] == "2026-10-16" and q["search"] == "坑"


def test_read_daily_query_param_errors(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    r = m._read_impl(_ctx("/proj"), scope="daily", level="bogus")
    assert not r.success and "非法等级" in (r.error or "")
    r2 = m._read_impl(_ctx("/proj"), scope="daily", date="2026-10-16", from_date="2026-10-10")
    assert not r2.success and "互斥" in (r2.error or "")
    r3 = m._read_impl(_ctx("/proj"), scope="daily", from_date="2026-10-16", to_date="2026-10-10")
    assert not r3.success and "不得晚于" in (r3.error or "")


def test_read_daily_query_no_hit(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    # 打桩：无命中（空 content）
    mgr.last_query = None
    orig = mgr.query_daily

    def _empty(
        aid,
        wd,
        from_date="",
        to_date="",
        levels=None,
        search="",
        summary_only=False,
        page=1,
        per_page=20,
    ):
        mgr.last_query = {
            "from_date": from_date,
            "to_date": to_date,
            "levels": list(levels or []),
            "search": search,
            "summary_only": summary_only,
            "page": page,
        }
        return {
            "ok": True,
            "content": "",
            "page": 1,
            "pages": 1,
            "total": 5,
            "matched": 0,
            "days": [],
            "search_mode": "text",
        }

    mgr.query_daily = _empty
    r = m._read_impl(_ctx("/proj"), scope="daily", search="不存在")
    assert r.success and "无命中" in r.content and "命中 0/5" in r.content
    assert "桩条目" not in r.content


def test_register_registers_both(monkeypatch):
    class _Reg:
        def __init__(self):
            self.names = []

        def register(self, name, schema, **kw):
            self.names.append(name)

    reg = _Reg()
    m.register(reg)
    assert reg.names == ["memory_read", "memory_write"]


def test_write_global_snapshots_before_write(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    r = m._write_impl(_ctx(), scope="global", content="新全局记忆")
    assert r.success
    assert ("build", "global") in mgr.snapshots


def test_write_project_snapshots_before_write(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    r = m._write_impl(_ctx("/proj"), scope="project", content="新项目记忆")
    assert r.success
    assert ("build", "project") in mgr.snapshots


# ── mode=append / 乐观锁 / 收缩确认 ──────────────────────────
def test_write_mode_append_global(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    r = m._write_impl(_ctx("/proj"), scope="global", content="规则 A", mode="append")
    assert r.success and "追加" in r.content
    r2 = m._write_impl(_ctx("/proj"), scope="global", content="规则 B", mode="append")
    assert r2.success
    r3 = m._read_impl(_ctx("/proj"), scope="global")
    assert "规则 A" in r3.content and "规则 B" in r3.content
    # 原有内容未被覆盖（append 语义：两条都在）
    assert r3.content.index("规则 A") < r3.content.index("规则 B")


def test_write_append_over_limit_rejected(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    mgr.write_global_memory("build", "x" * 3990)
    r = m._write_impl(_ctx("/proj"), scope="global", content="y" * 20, mode="append")
    assert not r.success and "超上限" in r.error


def test_write_replace_conflict(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    r0 = m._read_impl(_ctx("/proj"), scope="global")
    base0 = r0.content.rsplit("(base: ", 1)[1].rstrip(")")
    m._write_impl(_ctx("/proj"), scope="global", content="v1")
    # 仍带旧 base 写 → 冲突，错误里给当前 base
    r = m._write_impl(_ctx("/proj"), scope="global", content="v2", base=base0)
    assert not r.success
    assert "版本冲突" in r.error and "当前 base:" in r.error


def test_write_replace_shrink_requires_consolidate(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    m._write_impl(_ctx("/proj"), scope="global", content="- 条目若干内容填充\n" * 40)
    r = m._write_impl(_ctx("/proj"), scope="global", content="短条目")
    assert not r.success
    assert "consolidate=true" in r.error and "mode=append" in r.error
    r2 = m._write_impl(_ctx("/proj"), scope="global", content="精简版", consolidate=True)
    assert r2.success


def test_write_shrink_allowed_when_small_file(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    m._write_impl(_ctx("/proj"), scope="global", content="x" * 150)
    r = m._write_impl(_ctx("/proj"), scope="global", content="y")
    assert r.success  # 现有低于阈值不做收缩检查


def test_read_global_returns_base(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    r = m._read_impl(_ctx("/proj"), scope="global")
    assert "(base: " in r.content
    r2 = m._read_impl(_ctx("/proj"), scope="project")
    assert "(base: " in r2.content

# ── 全局记忆分区：memory_write 强制归属 ─────────────────────

_SECTIONS = [
    {"name": "工程方法论", "hint": "跨项目通用的做法"},
    {"name": "用户偏好", "hint": "表达方式与禁止事项"},
]


def test_write_append_requires_section_when_schema_defined(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    mgr.sections = _SECTIONS
    r = m._write_impl(_ctx("/proj"), scope="global", content="- 一条规则", mode="append")
    assert not r.success
    assert "必须带 section" in r.error
    assert "工程方法论" in r.error and "用户偏好" in r.error


def test_write_append_rejects_unknown_section(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    mgr.sections = _SECTIONS
    r = m._write_impl(_ctx("/proj"), scope="global", content="- 一条规则", mode="append", section="杂项")
    assert not r.success and "不在清单内" in r.error


def test_write_append_lands_in_section(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    mgr.sections = _SECTIONS
    mgr.write_global_memory("build", "## 工程方法论\n- 旧条目\n\n## 用户偏好\n- 旧偏好\n")
    r = m._write_impl(_ctx("/proj"), scope="global", content="- 新条目", mode="append", section="工程方法论")
    assert r.success and "工程方法论" in r.content
    text = mgr._g["build"]
    assert text.index("- 旧条目") < text.index("- 新条目") < text.index("## 用户偏好")


def test_write_replace_rejects_unknown_title(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    mgr.sections = _SECTIONS
    r = m._write_impl(_ctx("/proj"), scope="global", content="## 工程方法论\n- A\n\n## 缓存策略\n- B\n")
    assert not r.success
    assert "清单外标题" in r.error and "缓存策略" in r.error
    assert "dry_run=true" in r.error


def test_write_replace_accepts_wellformed(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    mgr.sections = _SECTIONS
    r = m._write_impl(_ctx("/proj"), scope="global", content="## 工程方法论\n- A\n\n## 用户偏好\n- B\n")
    assert r.success, r.error


def test_write_dry_run_does_not_persist(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    mgr.sections = _SECTIONS
    mgr.write_global_memory("build", "## 工程方法论\n- 旧\n")
    r = m._write_impl(_ctx("/proj"), scope="global", content="## 工程方法论\n- 新\n", dry_run=True)
    assert r.success and "dry_run" in r.content
    assert "- 旧" in mgr._g["build"] and "- 新" not in mgr._g["build"]
    assert mgr.snapshots == []


def test_write_dry_run_still_reports_invalid(monkeypatch, tmp_path):
    mgr = _patch(monkeypatch, tmp_path)
    mgr.sections = _SECTIONS
    r = m._write_impl(_ctx("/proj"), scope="global", content="## 乱写\n- X\n", dry_run=True)
    assert not r.success and "乱写" in r.error


def test_write_freeform_when_schema_empty(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path)
    # 空 schema：自由格式，append 不需 section、replace 不做标题校验
    r = m._write_impl(_ctx("/proj"), scope="global", content="随便一段", mode="append")
    assert r.success
    r2 = m._write_impl(_ctx("/proj"), scope="global", content="## 任意标题\n- X\n")
    assert r2.success
