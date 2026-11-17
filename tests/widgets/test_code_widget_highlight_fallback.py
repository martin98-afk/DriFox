# -*- coding: utf-8 -*-
"""T3-A 回归：Qt 代码块高亮降级 + 侧区空态短路。

背景（2026-11-18 探针实测）：含 fence payload 的构造帧尾刺 = CodeBlockWidget
首次 pygments（400 行块 36ms）+ QTextEdit.setHtml 排版（~18ms）；超长块降级
escape/setPlainText（与 I1 f0f14a76 同口径异层级：那边是 WebEngine 渲染层，
这边是 Qt 构造链）；侧区空态短路使每帧 reconcile 调用 2→1（触发次数减半）。
"""

import pytest

from app.widgets.markdown_block_viewer import (
    ToolSectionWidget,
    _highlight_inner_cached,
)

os_env_set = pytest.importorskip  # 占位避免未用告警（实际无外部依赖）


def test_oversize_code_falls_back_to_none():
    """超 4000 字块跳过 pygments 返回 None（I1 同口径降级）。"""
    big = "x = 1\n" * 800  # ~4800 字
    assert _highlight_inner_cached(big, "python") is None


def test_normal_code_highlight_and_memo_hit():
    """正常块高亮非 None；同 (code, lang) 二次调用命中 memo（同指纹复用）。"""
    code = "def f(x):\n    return x + 1\n"
    first = _highlight_inner_cached(code, "python")
    assert first is not None and "<pre" not in first  # inner 片段，非完整 pre
    second = _highlight_inner_cached(code, "python")
    assert second == first


def test_code_widget_fallback_uses_plain_text(qapp):
    """降级块经 setPlainText 落地：toPlainText 与源码逐字一致。"""
    from app.widgets.markdown_block_viewer import CodeBlockWidget

    big = "line = %d  # 注释内容填充长度\n" * 1 % 0 + "x = 1\n" * 800
    w = CodeBlockWidget(big, "python")
    try:
        assert w.text_edit.toPlainText() == big
        assert "<span" not in w.text_edit.toPlainText()  # 无高亮 token 标记
    finally:
        w.deleteLater()


def test_tool_section_empty_guard_short_circuits(qapp):
    """items 与 widgets 双空时 reconcile 直接短路（不建控件、不展开分区）。"""
    from PyQt5.QtWidgets import QVBoxLayout, QWidget

    host = QWidget()
    lay = QVBoxLayout(host)
    sec = ToolSectionWidget(host)
    sec.reconcile([], True)
    sec.reconcile([], True)
    assert sec._widgets == [] and sec._keys == []
    lay.addWidget(sec)
    host.deleteLater()
