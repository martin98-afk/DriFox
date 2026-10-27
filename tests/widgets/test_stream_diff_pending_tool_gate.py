# -*- coding: utf-8 -*-
"""流式差量渲染让位判据回归（成片闪现 / 白屏 / 无打字机根因）。

背景
----
``_has_active_tool_dom()`` 把「已完成工具集合非空」（``_restore_finished_ids``）
也算作活跃工具 DOM，而该集合在**本轮第一次工具结果落地后整轮非空**（只在
净化缓存时 clear）。于是「只要本轮调用过工具，后续每条正文都让位全量渲染」：
每个硬边界走一次 ``updateContent`` 整页 innerHTML 替换 → 成片闪现、白屏重排、
``_twReset`` 丢弃打字机缓冲（观感无打字机）。

修复把流式差量的让位判据收敛为 ``_has_pending_tool_dom()``：只有「JS 注入但
尚未落进 markdown 的工具块」（dirty / pending 运行框）才阻断。已完成工具的
块已在 markdown 中，差量只追加新闭合段、不动已有 DOM，不需要 restore 保护。

本测试锁定判据语义（无 Qt 依赖：方法只读属性，用 SimpleNamespace 桩）。
"""

from types import SimpleNamespace

from app.widgets.card_viewers import CodeWebViewer, _seg_has_protocol_block


def _viewer(**kwargs) -> SimpleNamespace:
    base = {
        "_tool_dom_dirty": False,
        "_injected_pending_tools": set(),
        "_restore_finished_ids": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_finished_tools_do_not_block_stream_diff():
    """已完成工具集合非空 → 流式差量仍可用（核心回归）。"""
    v = _viewer(_restore_finished_ids={"call_1", "call_2"})
    assert CodeWebViewer._has_pending_tool_dom(v) is False
    # 旧判据仍认为有活跃工具 DOM（收尾路径继续用它）
    assert CodeWebViewer._has_active_tool_dom(v) is True


def test_js_injected_running_box_blocks_stream_diff():
    """JS 注入的运行框（dirty）→ 必须让位全量 + save/restore。"""
    assert CodeWebViewer._has_pending_tool_dom(_viewer(_tool_dom_dirty=True)) is True


def test_pending_injected_tool_blocks_stream_diff():
    """pending 集合非空（注入未完成）→ 必须让位全量。"""
    v = _viewer(_injected_pending_tools={"call_1"})
    assert CodeWebViewer._has_pending_tool_dom(v) is True


def test_clean_viewer_allows_stream_diff():
    """无任何工具 DOM 痕迹 → 差量可用。"""
    assert CodeWebViewer._has_pending_tool_dom(_viewer()) is False


def test_seg_with_protocol_block_falls_back():
    """含 think / tool 协议的闭合段不做差量追加（防重复框）。"""
    assert _seg_has_protocol_block("<think>推理</think>\n\n") is True
    assert _seg_has_protocol_block("<tool>read\nargs: {}\n</tool>\n\n") is True


def test_plain_text_seg_goes_diff():
    """纯文本段走差量（流式输出的绝对多数）。"""
    assert _seg_has_protocol_block("这是一段正文，没有协议标签。\n\n") is False
    assert _seg_has_protocol_block("") is False
