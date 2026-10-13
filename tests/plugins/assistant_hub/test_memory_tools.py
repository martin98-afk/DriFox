# -*- coding: utf-8 -*-
"""test_memory_tools.py — memory_read / memory_write 工具回归（假 manager 注入）。"""

import importlib.util
import sys
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
        self._d = {}
        self._tmp = tmp_path

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

    def append_daily(self, aid, wd, text, date=""):
        date = date or "2026-10-13"
        self._d.setdefault((aid, wd, date), []).append(text)
        return {"ok": True, "date": date, "chars": sum(len(x) for x in self._d[(aid, wd, date)])}

    def list_daily(self, aid, wd, limit=7):
        items = [
            {"date": k[2], "chars": sum(len(x) for x in v)} for k, v in self._d.items() if k[0] == aid and k[1] == wd
        ]
        items.sort(key=lambda it: it["date"], reverse=True)
        return items[:limit]

    def read_daily(self, aid, wd, date=""):
        date = date or "2026-10-13"
        return "\n".join(self._d.get((aid, wd, date), []))

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
    _patch(monkeypatch, tmp_path)
    r = m._write_impl(_ctx("/proj"), scope="daily", content="- 完成了记忆重构")
    assert r.success and "追加" in r.content
    # 不带 date：返回近期日志索引
    r2 = m._read_impl(_ctx("/proj"), scope="daily")
    assert "2026-10-13" in r2.content
    # 带 date 精确读全文
    r3 = m._read_impl(_ctx("/proj"), scope="daily", date="2026-10-13")
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


def test_register_registers_both(monkeypatch):
    class _Reg:
        def __init__(self):
            self.names = []

        def register(self, name, schema, **kw):
            self.names.append(name)

    reg = _Reg()
    m.register(reg)
    assert reg.names == ["memory_read", "memory_write"]
