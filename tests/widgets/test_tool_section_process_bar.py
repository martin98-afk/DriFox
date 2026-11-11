# -*- coding: utf-8 -*-
"""「工具与思考」折叠区形态锚定：左对齐进程条（骨架 v62）。

背景（2026-10-30）：v61 给 #tool-section 加的圆角细边框围栏在真机上是「卡中卡」
（外层消息卡 + 围栏 + 工具卡三层圆角），标题又被 ::before/::after 两条横线顶到
居中，破坏正文左对齐轴。用户要求改成左对齐进程条：去外框、标题左对齐正文轴、
计数独立 pill、内容左侧竖线缩进。本文件锁定这四条，防止回退成居中/套框形态。
"""

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent.parent / "app" / "widgets" / "card_viewers.py"


def _src() -> str:
    return _SRC.read_text(encoding="utf-8")


def _rule_body(src: str, selector: str) -> str:
    """取 f-string 模板里某条规则的声明体（源码中花括号是 {{ }} 双写）。"""
    m = re.search(re.escape(selector) + r" \{\{(.*?)\}\}", src, re.S)
    assert m, f"骨架 CSS 里找不到规则 {selector}"
    return m.group(1)


def test_tool_section_has_no_outer_frame():
    """#tool-section 不得再有边框/圆角/内边距：外框交给外层消息卡。"""
    body = _rule_body(_src(), "#tool-section")
    assert "border: none;" in body
    assert "border-radius" not in body
    assert "padding: 0;" in body


def test_tool_separator_is_left_aligned():
    """标题行左对齐：无左右横线伪元素，内边距右侧留白、左侧对齐正文轴。"""
    src = _src()
    body = _rule_body(src, "#tool-separator")
    assert "padding: 5px 8px 5px 0;" in body
    assert "font-weight: 600;" in body
    assert "color: var(--text-secondary);" in body
    # fieldset 风的两条横线必须消失
    assert "#tool-separator::before" not in src
    assert "#tool-separator::after" not in src


def test_tool_content_has_left_rail():
    """内容区左侧竖线承担分区：margin-left + border-left + 左内边距缩进。"""
    body = _rule_body(_src(), "#tool-content")
    assert "border-left: 1.5px solid var(--border);" in body
    assert "margin-left: 7px;" in body
    assert "padding: 1px 0 1px 11px;" in body


def test_header_count_is_separate_pill():
    """标题与计数拆开：标题字符串恒定，项数只改 .ts-count 的内容与显隐。"""
    src = _src()
    assert '<span class="ts-label">工具与思考</span>' in src
    assert '<span class="ts-count"' in src
    # 旧实现把计数拼进标题 textContent，会把图标 span 一起覆盖
    assert "⚙ 工具与思考" not in src
    assert "':scope > span:not(.chevron)'" not in src
    assert "':scope > .ts-count'" in src


def test_count_pill_style():
    """计数药丸有独立样式（含等宽数字，避免项数变化时行宽抖动）。"""
    body = _rule_body(_src(), "#tool-separator .ts-count")
    assert "border-radius: 999px;" in body
    assert "font-variant-numeric: tabular-nums;" in body
