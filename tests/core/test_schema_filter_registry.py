# -*- coding: utf-8 -*-
"""test_schema_filter_registry.py — ToolRegistry schema 过滤器注册表测试。"""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# registry.py 无第三方重依赖，按路径加载避免全量 app 包初始化
_spec = importlib.util.spec_from_file_location(
    "test_schema_filter_registry_mod", str(_ROOT.parent / "app" / "tools" / "registry.py")
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("test_schema_filter_registry_mod", _mod)
_spec.loader.exec_module(_mod)

ToolRegistry = _mod.ToolRegistry


def _schema(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": name}}


def _fresh_registry() -> ToolRegistry:
    ToolRegistry.reset_instance()
    return ToolRegistry.get_instance()


def test_register_and_apply():
    reg = _fresh_registry()
    reg.register_schema_filter(
        "plug_a",
        lambda schemas, ctx: [s for s in schemas if s["function"]["name"] != "secret"],
    )
    out = reg.apply_schema_filters([_schema("read"), _schema("secret")], {"session_id": "s1"})
    assert [s["function"]["name"] for s in out] == ["read"]


def test_ctx_passthrough():
    reg = _fresh_registry()
    seen = {}

    def fn(schemas, ctx):
        seen.update(ctx)
        return schemas

    reg.register_schema_filter("plug_a", fn)
    reg.apply_schema_filters([_schema("read")], {"session_id": "abc"})
    assert seen == {"session_id": "abc"}


def test_overwrite_same_owner():
    reg = _fresh_registry()
    reg.register_schema_filter("plug_a", lambda schemas, ctx: [])
    reg.register_schema_filter("plug_a", lambda schemas, ctx: schemas)
    out = reg.apply_schema_filters([_schema("read")], {})
    assert len(out) == 1


def test_unregister_idempotent():
    reg = _fresh_registry()
    reg.register_schema_filter("plug_a", lambda schemas, ctx: [])
    reg.unregister_schema_filter("plug_a")
    reg.unregister_schema_filter("plug_a")  # 幂等
    out = reg.apply_schema_filters([_schema("read")], {})
    assert len(out) == 1


def test_broken_filter_skipped():
    """单个过滤器异常不拖垮整体（跳过继续）。"""
    reg = _fresh_registry()

    def boom(schemas, ctx):
        raise RuntimeError("x")

    reg.register_schema_filter("bad", boom)
    reg.register_schema_filter("good", lambda schemas, ctx: schemas[:1])
    out = reg.apply_schema_filters([_schema("a"), _schema("b")], {})
    assert [s["function"]["name"] for s in out] == ["a"]


def test_non_callable_rejected():
    reg = _fresh_registry()
    try:
        reg.register_schema_filter("plug_a", "not-callable")
        assert False, "应抛 TypeError"
    except TypeError:
        pass
