# -*- coding: utf-8 -*-
"""test_prompts.py — prompt 模板 sanity 检查。"""

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


def test_compile_today():
    c = _roles_ok(m.build_compile_today("新对话内容", "旧草稿"))
    assert "已有草稿" in c and "新对话" in c


def test_compile_daily():
    assert "蒸馏" in _roles_ok(m.build_compile_daily("昨天的草稿"))


def test_compile_facts():
    c = _roles_ok(m.build_compile_facts("已有事实", "新对话"))
    assert "重要事实" in c


def test_dream_chain():
    a = _roles_ok(m.build_dream_atomize("记忆文本"))
    assert "原子" in a
    d = _roles_ok(m.build_dream_dedupe("- 单元1"))
    assert "去重" in d
    o = _roles_ok(m.build_dream_optimize("- 单元1"))
    assert "优化" in o
    c = _roles_ok(m.build_dream_compose("- 单元1"))
    assert "长期记忆" in c
    v = _roles_ok(m.build_dream_verify("整理前", "整理后"))
    assert "semantic_ok" in v


def test_reflect():
    c = _roles_ok(m.build_reflect("人格", "记忆", "经验"))
    assert "JSON" in c and "category" in c
