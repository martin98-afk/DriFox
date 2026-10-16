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


def test_memory_system_contains_distill_rules():
    """说明书含蒸馏流程 + 全局严选标准；触发时机归程序判断，不写静态说教。"""
    text = m.build_memory_system("/data/MEMORY.md", "/proj/.drifox/memory/build")
    assert "新日门禁" not in text  # 触发时机由程序在 memory_write 时判断，说明书不写
    assert "memory_write 执行时判断" in text
    assert "mark_distilled" in text
    assert ".distilled" in text
    assert "最珍贵" in text and "项目无关" in text
    # 全局准入：显式要求 + 蒸馏严选两类
    assert "蒸馏" in text and "宁缺勿滥" in text


def test_build_distill_gate():
    """蒸馏门禁指令：待蒸馏日期 + 校对/严选/打标步骤齐全。"""
    c = m.build_distill_gate(["2026-10-14", "2026-10-15"], "/proj/MEMORY.md", "/data/MEMORY.md")
    assert "2026-10-14" in c and "2026-10-15" in c
    assert "memory_read(scope=daily, date=2026-10-14)" in c
    assert "memory_write(scope=daily, date=2026-10-15, mark_distilled=true)" in c
    assert "校对" in c
    assert "最珍贵" in c and "项目无关" in c and "宁缺勿滥" in c
    assert "/proj/MEMORY.md" in c and "/data/MEMORY.md" in c


def test_reflect():
    c = _roles_ok(m.build_reflect("人格", "记忆", "经验"))
    assert "JSON" in c and "category" in c


def test_consolidate():
    c = _roles_ok(m.build_consolidate([{"category": "测试", "content": "一条经验"}]))
    assert "JSON" in c and "测试" in c and "一条经验" in c
