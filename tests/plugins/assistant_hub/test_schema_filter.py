# -*- coding: utf-8 -*-
"""test_schema_filter.py — assistant_hub 工具权限档位过滤测试。"""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "tools" / "schema_filter.py"

spec = importlib.util.spec_from_file_location("test_assistant_schema_filter_mod", str(_MODULE))
m = importlib.util.module_from_spec(spec)
sys.modules.setdefault("test_assistant_schema_filter_mod", m)
spec.loader.exec_module(m)

# 直接 import：schema_filter.register() 内部就是从 app.tools.registry 拿单例，
# 测试必须用同一份模块对象才能断言到注册结果
from app.tools.registry import ToolRegistry as _RealRegistry


class _A:
    tool_access = "full"


class _Mgr:
    aid = "a1"
    mode = "full"

    def active_id(self):
        return self.aid

    def get(self, aid):
        a = _A()
        a.tool_access = self.mode
        return a

    def tool_access_for(self, sid):
        return self.mode


def _schemas():
    return [
        {"type": "function", "function": {"name": n, "description": n}}
        for n in ("read", "grep", "bash", "write", "edit", "recall_experience")
    ]


def _patch_manager(monkeypatch, mode):
    mgr = _Mgr()
    mgr.mode = mode
    monkeypatch.setattr(m, "_get_manager", lambda: mgr)
    return mgr


def test_full_no_filter(monkeypatch):
    _patch_manager(monkeypatch, "full")
    schemas = _schemas()
    out = m.filter_tools_schema(schemas, {"session_id": "s1"})
    assert out is schemas and len(out) == 6


def test_minimal_only_bash(monkeypatch):
    _patch_manager(monkeypatch, "minimal")
    out = m.filter_tools_schema(_schemas(), {"session_id": "s1"})
    assert [s["function"]["name"] for s in out] == ["bash"]


def test_readonly_only_safe(monkeypatch):
    _patch_manager(monkeypatch, "readonly")
    out = m.filter_tools_schema(_schemas(), {"session_id": "s1"})
    names = [s["function"]["name"] for s in out]
    assert set(names) == {"read", "grep", "recall_experience"}


def test_no_manager_passthrough(monkeypatch):
    monkeypatch.setattr(m, "_get_manager", lambda: None)
    schemas = _schemas()
    out = m.filter_tools_schema(schemas, {"session_id": "s1"})
    assert out is schemas


def test_register_wires_registry(monkeypatch):
    """register() 把过滤器挂到 app.tools.registry 的 ToolRegistry 单例。"""
    _RealRegistry.reset_instance()
    reg = _RealRegistry.get_instance()
    m.register(None)  # 参数仅占位（真实入口传 proxy，这里直接用单例）
    assert "assistant_hub" in reg._schema_filters
    # 全链路：full 模式不过滤
    _patch_manager(monkeypatch, "full")
    out = reg.apply_schema_filters(_schemas(), {"session_id": "s1"})
    assert len(out) == 6
    _RealRegistry.reset_instance()
