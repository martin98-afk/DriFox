# -*- coding: utf-8 -*-
"""流式围栏检测：保持逐 chunk 语义，正文行数不放大 Python 切片次数。"""

import re

import pytest

from app.widgets import card_render_core as core


class _TrackedText(str):
    def __new__(cls, value):
        obj = super().__new__(cls, value)
        obj.slices = 0
        return obj

    def __getitem__(self, key):
        if isinstance(key, slice):
            self.slices += 1
        return super().__getitem__(key)


def _reference(text, languages):
    """优化前的逐行状态机，独立保留为等价性参照。"""
    if re.search(r"(?:^|\n)[ \t]{0,3}[`~]{1,3}[a-zA-Z0-9]{0,15}$", text[-32:]):
        return True
    inside = False
    chart = False
    marker = ""
    for line in text.split("\n"):
        stripped = line.strip()
        if not inside:
            match = re.match(r"^\s*(```|~~~)\s*(\w+)", stripped)
            if match:
                inside = True
                marker = match.group(1)
                chart = match.group(2).lower() in languages
            elif stripped.startswith(("```", "~~~")):
                inside = True
                marker = stripped[:3]
                chart = False
        elif stripped.startswith(marker):
            inside = False
            chart = False
    return inside and chart


def test_prose_lines_do_not_create_per_line_slices(monkeypatch):
    """一次真实检测只处理围栏候选，不能为上千行旧正文逐行分配字符串。"""
    monkeypatch.setattr(core, "_is_render_fence_lang", lambda lang: lang == "echarts")
    text = _TrackedText(
        "普通正文与行内 `代码`，这里没有围栏。\n" * 3000
        + "```python\nx = 1\n```\n\n```echarts\n{}\n"
    )
    assert core._has_unclosed_chart_fence(text)
    assert text.slices <= 8, f"一次检测切片 {text.slices} 次，仍在逐行扫描旧正文"


@pytest.mark.parametrize("chunk_size", [1, 7, 40])
@pytest.mark.parametrize(
    "text",
    [
        "正文\n\n```echarts\n{\"x\": 1}\n```\n后文\n",
        "```python\nprint('```mermaid')\n~~~\n```\n~~~mermaid\ngraph TD; A-->B",
        "\t```python\nx = 1\n\t```\n\u3000~~~plugin_chart\n待渲染\n~~~\n结束",
        "正文中```echarts不是行首\n\n```\n普通代码\n```\n",
        "\n\n\r\t\n~~~html\n<div>内容</div>\n\n~~~\n尾部``",
        "```python\n\n```\n\n```widget\n{\"type\": \"html\"}",
    ],
)
def test_all_stream_prefixes_match_original_state_machine(monkeypatch, text, chunk_size):
    languages = {"echarts", "mermaid", "html", "widget", "svg", "plugin_chart"}
    monkeypatch.setattr(core, "_is_render_fence_lang", lambda lang: lang.lower() in languages)
    for end in [*range(0, len(text), chunk_size), len(text)]:
        prefix = text[:end]
        assert core._has_unclosed_chart_fence(prefix) == _reference(prefix, languages), repr(prefix)
