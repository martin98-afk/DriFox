# -*- coding: utf-8 -*-
"""test_experience_tools.py — 经验工具注册与 impl 测试。"""
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "tools" / "experience_tools.py"

spec = importlib.util.spec_from_file_location("test_exp_tools_mod", str(_MODULE))
m = importlib.util.module_from_spec(spec)
sys.modules.setdefault("test_exp_tools_mod", m)
spec.loader.exec_module(m)


class _A:
    experience_enabled = True
    name = "小狐"


class _Mgr:
    aid = "a1"

    def active_id(self):
        return self.aid

    def has(self, aid):
        return bool(aid)

    def get(self, aid):
        return _A() if aid else None

    def experience_read_index(self, aid):
        return "# 经验索引"

    def experience_read(self, aid, category):
        return f"# {category}\n\n- 条目"

    def experience_record(self, aid, category, content):
        return {"added": True}


class _Registry:
    def __init__(self):
        self.registered = {}

    def register(self, name, schema, **kw):
        self.registered[name] = {"schema": schema, **kw}


def test_register_schemas(monkeypatch):
    monkeypatch.setattr(m, "_get_manager", lambda: None)
    reg = _Registry()
    m.register(reg)
    assert set(reg.registered) == {"recall_experience", "record_experience"}
    r = reg.registered["recall_experience"]
    assert r["group"] == "助手记忆" and r["danger"] == "safe"
    assert "category" in r["schema"]["function"]["parameters"]["properties"]
    rec = reg.registered["record_experience"]
    assert rec["schema"]["function"]["parameters"]["required"] == ["category", "content"]


def test_recall_index_and_category(monkeypatch):
    mgr = _Mgr()
    monkeypatch.setattr(m, "_get_manager", lambda: mgr)
    out = m._recall_impl({})
    assert "经验索引" in out.content
    out2 = m._recall_impl({}, category="代码风格")
    assert "代码风格" in out2.content


def test_record(monkeypatch):
    mgr = _Mgr()
    monkeypatch.setattr(m, "_get_manager", lambda: mgr)
    out = m._record_impl({}, category="工作流", content="先跑 ruff")
    assert out.content.startswith("已记录")
    # 缺参数
    out2 = m._record_impl({}, category="", content="")
    assert out2.success is False


def test_paused_when_disabled(monkeypatch):
    mgr = _Mgr()
    monkeypatch.setattr(m, "_get_manager", lambda: mgr)

    class _A2(_A):
        experience_enabled = False

    mgr.get = lambda aid: _A2() if aid else None
    out = m._recall_impl({})
    assert "暂停" in out.content
    out2 = m._record_impl({}, category="x", content="y")
    assert "暂停" in out2.content


def test_no_active_assistant(monkeypatch):
    mgr = _Mgr()
    mgr.aid = ""
    monkeypatch.setattr(m, "_get_manager", lambda: mgr)
    out = m._recall_impl({})
    assert "没有激活的助手" in out.content
