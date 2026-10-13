# -*- coding: utf-8 -*-
"""test_prompts.py — prompt 模板 sanity 检查（记忆说明书 + 经验）。"""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "core" / "memory" / "prompts.py"

spec = importlib.util.spec_from_file_location("test_prompts_mod", str(_MODULE))
m = importlib.util.module_from_spec(spec)
sys.modules.setdefault("test_prompts_mod", m)
spec.loader.exec_module(m)


def _roles_ok(msgs):
    assert msgs[0]["role"] == "system"
    assert all(set(x) == {"role", "content"} for x in msgs)
    return msgs[-1]["content"]


def test_memory_system_contains_layers_and_paths():
    """记忆说明书：两层结构 + 实际路径 + 写入时机 + 维护规则，且不含 workbuddy 品牌词。"""
    text = m.build_memory_system("/data/MEMORY.md", "/proj/.drifox/memory/build")
    assert "# 第一层 — 全局记忆" in text
    assert "# 第二层 — 项目记忆" in text
    assert "/data/MEMORY.md" in text
    assert "/proj/.drifox/memory/build" in text
    assert "memory_write" in text and "memory_read" in text
    assert "何时写" in text and "维护" in text
    assert "workbuddy" not in text.lower()
    assert "drifox" in text.lower()


def test_reflect():
    c = _roles_ok(m.build_reflect("人格", "记忆", "经验"))
    assert "JSON" in c and "category" in c


def test_consolidate():
    c = _roles_ok(m.build_consolidate([{"category": "测试", "content": "一条经验"}]))
    assert "JSON" in c and "测试" in c and "一条经验" in c
