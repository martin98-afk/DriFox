# -*- coding: utf-8 -*-
"""test_memory_migrate.py — v1 MEMORY.md → v2 事件与 legacy 段。"""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_BASE = _ROOT / "plugins" / "assistant_hub" / "core" / "memory"
_spec = importlib.util.spec_from_file_location("test_memory_migrate_mod", str(_BASE / "migrate.py"))
migrate = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("test_memory_migrate_mod", migrate)
_spec.loader.exec_module(migrate)

SECTIONS = [
    {"name": "工具与环境", "hint": "工具坑", "detail": "工具自身行为与平台坑"},
    {"name": "用户偏好", "hint": "他是什么人", "detail": "稳定理解"},
]


def test_parses_section_bullets():
    text = "# 全局\n\n## 工具与环境\n- Windows 换行坑\n- 插件 deps 平台\n\n## 用户偏好\n- 他要中文\n"
    r = migrate.parse_legacy(text, SECTIONS)
    assert r["stats"]["entries"] == 3
    assert {e["section"] for e in r["events"]} == {"工具与环境", "用户偏好"}
    assert all(e["origin"] == "import" for e in r["events"])
    assert all(e["source"]["kind"] == "migration" for e in r["events"])


def test_unknown_section_goes_to_legacy():
    text = "## 杂项\n- 一条无处安放的内容\n"
    r = migrate.parse_legacy(text, SECTIONS)
    assert r["stats"]["entries"] == 0
    assert "一条无处安放的内容" in r["legacy"][0]["text"]


def test_prose_and_subheaders_preserved_in_legacy():
    text = "## 工具与环境\n一些散文说明\n### 小节\n- 正常条目\n"
    r = migrate.parse_legacy(text, SECTIONS)
    assert r["stats"]["entries"] == 1
    joined = "\n".join(x["text"] for x in r["legacy"])
    assert "一些散文说明" in joined and "### 小节" in joined


def test_empty_and_blank_input():
    assert migrate.parse_legacy("", SECTIONS)["stats"] == {"entries": 0, "legacy": 0, "sections": 0}
    assert migrate.parse_legacy("   \n\n", SECTIONS)["stats"]["entries"] == 0


def test_crlf_input():
    r = migrate.parse_legacy("## 工具与环境\r\n- CRLF 条目\r\n", SECTIONS)
    assert r["events"][0]["text"] == "CRLF 条目"