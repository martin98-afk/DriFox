# -*- coding: utf-8 -*-
"""test_memory_tools_contract.py — memory_write 的 v2 参数契约。"""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_TOOLS = _ROOT / "plugins" / "assistant_hub" / "tools" / "memory_tools.py"
_spec = importlib.util.spec_from_file_location("test_memory_tools_contract_mod", str(_TOOLS))
tools = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("test_memory_tools_contract_mod", tools)
_spec.loader.exec_module(tools)


def test_schema_exposes_id_modes():
    schema = tools._WRITE_SCHEMA
    props = schema["function"]["parameters"]["properties"]
    assert "update" in props["mode"]["description"]
    assert "delete" in props["mode"]["description"]
    assert "id" in props
    assert "confirm" in props


def test_update_requires_id_and_content():
    assert tools._validate_v2_args({"mode": "update", "content": "x"}) == "update 需要 id"
    assert tools._validate_v2_args({"mode": "update", "id": "mabc123", "content": "x"}) == ""
    assert tools._validate_v2_args({"mode": "delete", "id": "mabc123"}) == ""
    assert "delete 需要 id" in tools._validate_v2_args({"mode": "delete"})
    assert tools._validate_v2_args({"mode": "delete", "section": "工具", "index": 1}) == ""


def test_merge_and_plan_validation():
    assert tools._validate_v2_args({"mode": "merge", "ids": ["m1"]}) == "merge 需要至少两个 ids"
    assert tools._validate_v2_args({"mode": "merge", "ids": ["m1", "m2"]}) == ""
    assert tools._validate_v2_args({"mode": "plan"}) == "plan 需要 plan 数组"
    assert tools._validate_v2_args({"mode": "plan", "plan": []}) == ""