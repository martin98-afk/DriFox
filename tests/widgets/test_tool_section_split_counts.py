# -*- coding: utf-8 -*-
"""「工具与思考」折叠区头部计数拆分：思考 / 操作 / 失败操作。

背景（2026-11-12）：头部此前只有一个合并计数「N 项」，思考块与工具块混在一起，
看不出失败了几次调用。改为三段：
    3 次思考 · 5 次操作 · 1 次失败操作
0 值段隐藏（无失败时不显示失败段）。

失败判据：工具块根节点 `data-tool-success="false"`。该属性只在渲染期
success is False 时输出（render_helpers._tool_success_attr）——运行中块
（success=None / 流式块）不得带标，否则失败数会虚高。
"""

import re
from pathlib import Path

import pytest

_WIDGETS = Path(__file__).resolve().parents[2] / "app" / "widgets"


def _src(name: str) -> str:
    return (_WIDGETS / name).read_text(encoding="utf-8")


# ── 渲染端：失败标记只打在失败块上 ──────────────────────────────


def _render(success, **kwargs):
    from app.widgets.render_helpers import render_tool_block

    return render_tool_block(
        "bash", {"command": "ls"}, "out", success, tool_call_id="tc1", **kwargs
    )


def test_failure_marker_only_on_failed_blocks():
    assert 'data-tool-success="false"' in _render(False, collapsed=True)
    assert 'data-tool-success="false"' not in _render(True, collapsed=True)
    # 运行中/未知（success=None）不得算失败
    assert 'data-tool-success="false"' not in _render(None, collapsed=True)


def test_failure_marker_on_all_render_branches():
    """折叠/展开/懒注入/无折叠四个分支都要带标记，漏一个分支就漏统计。"""
    for kwargs in (
        {"collapsed": True},
        {"collapsed": False},
        {"collapsed": True, "lazy_body": True},
        {"collapsed": False, "lazy_body": True},
    ):
        html = _render(False, **kwargs)
        assert 'data-tool-success="false"' in html, f"分支 {kwargs} 丢失败标记"


def test_failure_marker_on_inline_render_mode(monkeypatch):
    """render_mode="inline" 的紧凑单行卡也要带标记。"""
    from app.tools.registry import ToolRegistry
    from app.widgets.render_helpers import render_tool_block

    monkeypatch.setattr(ToolRegistry.get_instance(), "get_render_mode", lambda _n: "inline")
    html = render_tool_block("read", {"path": "a.py"}, "out", False, tool_call_id="tc1")
    assert 'data-tool-success="false"' in html


def test_markdown_path_propagates_failure():
    """历史/整串 markdown 渲染链（协议文本 → render_tool_block）不得丢标记。"""
    from app.widgets.card_render_core import _render_tool_block_content

    proto = "name: bash\nargs: {}\nresult: oops\nsuccess: False"
    html = _render_tool_block_content(proto, compact=True)
    assert 'data-tool-success="false"' in html


# ── WebEngine 骨架：三段计数逻辑 ───────────────────────────────


def _header_js() -> str:
    src = _src("card_viewers.py")
    start = src.index("function _updateToolSectionHeader()")
    end = src.index("自动展开：流式时有新工具且当前折叠", start)
    return src[start:end]


def test_skeleton_counts_three_kinds():
    block = _header_js()
    assert ".think-block" in block and ".think-streaming" in block and ".think-compact" in block
    assert "'.tool-block'" in block
    assert 'data-tool-success="false"' in block
    assert "' 次思考'" in block or "次思考" in block
    assert "次操作" in block
    assert "次失败操作" in block


def test_skeleton_hides_zero_segments():
    block = _header_js()
    # 只有 >0 才 push，最后 join —— 0 值段必须不出现在文本里
    assert re.search(r"if \(thinkN > 0\)", block)
    assert re.search(r"if \(toolN > 0\)", block)
    assert re.search(r"if \(failN > 0\)", block)
    assert "join(' · ')" in block


def test_skeleton_version_bumped():
    src = _src("card_render_core.py")
    idx = src.index("_SKELETON_CACHE_VERSION = ")
    version = int(src[idx:].split("=", 1)[1].strip().split("\n", 1)[0])
    assert version >= 79, f"三段计数上线后骨架版本须 >=79，实际 {version}"


# ── Qt 渲染器：同口径三段计数 ──────────────────────────────────


def test_qt_renderer_counts_three_kinds(qapp):
    from app.widgets.markdown_block_viewer import ToolSectionWidget, _slot_items

    section = ToolSectionWidget()
    blocks = [
        {"type": "think", "content": "t1", "completed": True},
        {"type": "think", "content": "t2", "completed": True},
        {"type": "tool", "name": "bash", "cn_name": "终端", "args_raw": "{}",
         "result": "ok", "success": True, "closed": True},
        {"type": "tool", "name": "bash", "cn_name": "终端", "args_raw": "{}",
         "result": "bad", "success": False, "closed": True},
    ]
    section.reconcile(_slot_items(blocks), streaming=False)
    text = section._separator._extra.text()
    assert text == "2 次思考 · 2 次操作 · 1 次失败操作", text


def test_qt_renderer_hides_zero_segments(qapp):
    from app.widgets.markdown_block_viewer import ToolSectionWidget, _slot_items

    section = ToolSectionWidget()
    section.reconcile(
        _slot_items([
            {"type": "tool", "name": "read", "cn_name": "读取", "args_raw": "{}",
             "result": "ok", "success": True, "closed": True},
        ]),
        streaming=False,
    )
    assert section._separator._extra.text() == "1 次操作"


def test_qt_renderer_empty_hides_pill(qapp):
    from app.widgets.markdown_block_viewer import ToolSectionWidget

    section = ToolSectionWidget()
    section.reconcile([], streaming=False)
    assert section._separator._extra.text() == ""
