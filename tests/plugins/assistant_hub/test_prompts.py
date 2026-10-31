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
    """说明书对齐 WorkBuddy Layer 2/3：分层纪律 + 路径 + 上限；蒸馏留给工具门禁。"""
    text = m.build_memory_system("/data/MEMORY.md", "/proj/.drifox/memory/build", {"global": 4000, "project": 3000})
    assert "memory_read" in text and "memory_write" in text
    assert "/data/MEMORY.md" in text
    assert "/proj/.drifox/memory/build" in text
    assert "工具说明" in text
    # 分层纪律：global 何时写 + project/daily 结构与上限
    assert "scope=global" in text and "4000" in text
    assert "scope=project" in text and "3000" in text
    assert "只追加" in text and "实质工作" in text
    # 检索选源与角色边界
    assert "全局记忆" in text and "角色边界" in text
    # 品牌词不出现；蒸馏纪律零提及（由工具门禁注入）
    assert "workbuddy" not in text.lower()
    assert "蒸馏" not in text
    assert "mark_distilled" not in text and ".distilled" not in text


def test_build_distill_gate():
    """蒸馏门禁指令：待蒸馏日期 + 校对/严选/打标步骤齐全；打标为单条全量命令（date 已从写面移除）"""
    c = m.build_distill_gate(["2026-10-14", "2026-10-15"], "/proj/MEMORY.md", "/data/MEMORY.md")
    assert "2026-10-14" in c and "2026-10-15" in c
    assert "memory_read(scope=daily, date=2026-10-14)" in c
    assert "memory_write(scope=daily, mark_distilled=true)" in c
    assert "date=2026-10-15, mark_distilled" not in c, "打标不再逐日传 date"
    assert "校对" in c
    assert "最珍贵" in c and "项目无关" in c and "宁缺勿滥" in c
    assert "/proj/MEMORY.md" in c and "/data/MEMORY.md" in c




