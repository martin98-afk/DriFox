# -*- coding: utf-8 -*-
"""test_system_tools_mcp_render — mcp_manage 临时开启成功的名录式渲染单测。

背景：渲染曾把全部工具的完整 Parameters JSON 灌给模型（一台 15 工具的
服务器可达数万字符）。改为名录式（名 + 一句话描述）后，参数详情由模型
用 tool_search 的 tool_names 按需查询。
"""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MODULE = _ROOT / "plugins" / "system-tools" / "tools" / "interaction_tools.py"

spec = importlib.util.spec_from_file_location("test_system_tools_interaction_mod", str(_MODULE))
m = importlib.util.module_from_spec(spec)
sys.modules.setdefault("test_system_tools_interaction_mod", m)
spec.loader.exec_module(m)


def _schema(name: str, desc: str, params: dict | None = None) -> dict:
    fn = {"name": name, "description": desc}
    if params is not None:
        fn["parameters"] = params
    return {"type": "function", "function": fn}


def test_catalog_style_without_parameters():
    """名录式：名 + 描述一行一个；不再灌完整 Parameters JSON"""
    out = m._mcp_render_enabled(
        "browser",
        [
            _schema("mcp__browser__navigate", "打开网页\n第二行不该出现", {"type": "object", "properties": {"url": {}}}),
            _schema("mcp__browser__click", "点击元素"),
        ],
    )
    assert "- mcp__browser__navigate：打开网页" in out
    assert "第二行" not in out, "多行描述只取首行"
    assert "Parameters" not in out and '"url"' not in out, "参数详情不进名录（按需 tool_search 查）"
    assert "tool_search" in out, "必须引导模型去 tool_search 查参数"
    assert 'mcp_manage(action="disable", name="browser")' in out


def test_long_description_truncated():
    out = m._mcp_render_enabled("srv", [_schema("t", "长" * 200)])
    assert ("长" * 100 + "…") in out
    assert ("长" * 101) not in out


def test_empty_schemas_hint():
    out = m._mcp_render_enabled("srv", [])
    assert "未发现任何工具" in out
