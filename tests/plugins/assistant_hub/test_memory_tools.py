# -*- coding: utf-8 -*-
"""test_memory_tools.py — memory_read / memory_write 工具回归（假 manager 注入）。

覆盖：基本读写、蒸馏门禁（新日首写拦截/放行）、打标（mark_distilled）、
单日条目分页、索引 limit。
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "tools" / "memory_tools.py"

spec = importlib.util.spec_from_file_location("test_memory_tools_mod", str(_MODULE))
m = importlib.util.module_from_spec(spec)
sys.modules.setdefault("test_memory_tools_mod", m)
spec.loader.exec_module(m)


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
        self.snapshots = []  # (aid, kind) — 写前快照调用记录
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

    def snapshot_memory(self, aid, kind, workdir=""):
        self.snapshots.append((aid, kind))
        return {"ok": True, "file": "snap.md"}

    def memory_paths(self, aid, wd=""):
        out = {"global": str(self._tmp / "MEMORY.md")}
        if wd:
            out["project"] = str(self._tmp / "proj" / "MEMORY.md")
        return out


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
    _patch(monkeypatch, tmp_path)
    m._write_impl(_ctx("/proj"), scope="daily", content="- x", date="2026-10-13")
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
    # 隔天补录（显式 date=过去）：不走门禁
    m._write_impl(_ctx("/proj"), scope="daily", content="- 补录", date="2020-01-01")
    assert mgr.gate_calls == 0
    # 当天已写过一条后，第二条不再拦截
    mgr._d[(mgr.active_id(), "/proj", today)] = ["- 首条"]
    r = m._write_impl(_ctx("/proj"), scope="daily", content="- 第二条")
    assert r.success and mgr.gate_calls == 0


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
    today = mgr._today()
    m._write_impl(_ctx("/proj"), scope="daily", content="- 甲")
    # 缺 date 拒绝；content 允许为空
    r = m._write_impl(_ctx("/proj"), scope="daily", mark_distilled=True)
    assert not r.success and "date" in (r.error or "")
    r2 = m._write_impl(_ctx("/proj"), scope="daily", date=today, mark_distilled=True)
    assert r2.success and "打蒸馏标" in r2.content
    # 重复打标失败
    r3 = m._write_impl(_ctx("/proj"), scope="daily", date=today, mark_distilled=True)
    assert not r3.success and "已打" in (r3.error or "")
    assert (mgr.active_id(), "/proj", today) in mgr._distilled


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
    for d in ["2026-10-01", "2026-10-02", "2026-10-03"]:
        m._write_impl(_ctx("/proj"), scope="daily", content=f"- {d}", date=d)
    r = m._read_impl(_ctx("/proj"), scope="daily", limit=2)
    assert "2026-10-03" in r.content and "2026-10-02" in r.content and "2026-10-01" not in r.content
    # 已蒸馏标注
    m._write_impl(_ctx("/proj"), scope="daily", date="2026-10-03", mark_distilled=True)
    r2 = m._read_impl(_ctx("/proj"), scope="daily")
    assert "已蒸馏" in r2.content


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
