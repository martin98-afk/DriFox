# -*- coding: utf-8 -*-
"""回归测试：工具运行折叠框 → 完成框 的关联修复（Python 侧不变量）。

根因
----
工具运行折叠框由 ``_find_latest_assistant_card()`` 创建，而工具结果由
``self._current_assistant_card`` 写入。当两者指向不同卡片时，运行框无法被
原地转换为完成框，并会持续累积，且消息内容仍在正常更新。

修复分两层：
  1. ``main_widget`` 用 ``_tool_card_map`` 记录 ``tool_call_id -> 卡片``，
     确保结果写入与运行框同一张卡片（路由层）。
  2. ``message_card`` 把 ``_finished_streaming_ids`` 共享给 viewer 的
     ``_restore_finished_ids``；全量重渲染（``_perform_update``）的 restore
     兜底逻辑若发现某已完成工具的运行框被“复活”，则强制转为完成态（防御层）。

第 1 层的路由逻辑与第 2 层的 JS 兜底需真实 Qt/Chromium 环境，由人工验证；
本测试覆盖第 2 层在 Python 侧的两条不变量：
  * ``append_tool_result`` 必须把 ``tool_call_id`` 标记为已完成；
  * 该已完成集合必须同步给 ``viewer._restore_finished_ids``，供 restore 兜底使用。
"""
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


class _StubViewer:
    """无头 viewer 桩：吞掉 runJavaScript / _schedule_render，记录调用。"""

    def __init__(self):
        self._streaming = False
        self._restore_finished_ids = None
        self._schedule_render_calls = 0
        self.js_calls = []
        self._tool_target_id = "content-placeholder"  # 非简洁模式默认值
        self._tool_compact_mode = False  # 非简洁模式（与真实 viewer 默认一致）

    def _schedule_render(self, immediate=False):
        self._schedule_render_calls += 1

    def page(self):
        return self

    def runJavaScript(self, js_code):
        self.js_calls.append(js_code)


def _make_card():
    from app.widgets.message_card import MessageCard

    _ensure_qapp()
    card = MessageCard(role="assistant")
    # 跳过“未懒渲染 / 无 viewer → 提前返回”分支，进入增量注入路径
    card._lazy_rendered = True
    card.viewer = _StubViewer()
    return card


def test_append_tool_result_marks_id_finished_and_propagates_to_viewer():
    """append_tool_result 必须把 tool_call_id 标记为已完成，并同步给 viewer。"""
    card = _make_card()
    tid = "call_abc123"

    card.append_tool_result(
        tool_name="read_file",
        arguments={"path": "x.py"},
        result="hello",
        success=True,
        tool_call_id=tid,
    )

    # 1) 该工具 id 被记录为已完成（防御层据此判断运行框是否可复活）
    assert tid in card._finished_streaming_ids
    # 2) viewer 拿到了已完成集合的引用，供 _perform_update 的 restore 兜底使用
    assert card.viewer._restore_finished_ids is card._finished_streaming_ids
    # 3) 增量注入 JS 确实发往 viewer（说明走的是“原地转换”而非提前返回）
    assert len(card.viewer.js_calls) >= 1


def test_append_tool_result_without_tool_call_id_does_not_track():
    """没有 tool_call_id 时不污染 _finished_streaming_ids。"""
    card = _make_card()
    before = set(card._finished_streaming_ids)
    card.append_tool_result(tool_name="read_file", arguments={}, result="ok")
    assert card._finished_streaming_ids == before


def test_has_active_tool_dom_false_when_tool_done_and_dom_clean():
    """回归：工具已完成且 DOM 无**运行中**工具块时，_has_active_tool_dom() 必须返回 False。

    两层根因（任一都会让工具调用后的正文流式差量渲染永久让位全量渲染 →
    纯文本滞留到流式结束才刷新成 HTML）：
    1. _restore_finished_ids（= _finished_streaming_ids 引用，只 add、cleanup 才
       clear）曾被当作"活跃工具 DOM"判据；
    2. _tool_dom_dirty 清除依赖全量渲染应用成功的 JS 回调，流式期间全量结果被
       seq 过期丢弃时 dirty 长期 True，也曾被当作"活跃工具 DOM"判据。
    真正表示"JS 注入运行中工具块在 DOM"的是 _injected_pending_tools。
    """
    from app.widgets.message_card import CodeWebViewer

    viewer = _StubViewer()
    viewer._tool_dom_dirty = False
    viewer._injected_pending_tools = set()
    # 模拟真实链路：append_tool_result 同步给 viewer 的已完成集合（永不清空）
    viewer._restore_finished_ids = {"call_done_1"}
    assert CodeWebViewer._has_active_tool_dom(viewer) is False

    # 对照：dirty 单独置 True（全量渲染需 save/restore 保护）不应阻塞差量渲染
    viewer._tool_dom_dirty = True
    viewer._injected_pending_tools = set()
    assert CodeWebViewer._has_active_tool_dom(viewer) is False

    # 对照：DOM 确有 JS 注入的**运行中**工具块时仍必须让位差量渲染
    viewer._tool_dom_dirty = True
    viewer._injected_pending_tools = {"call_running_2"}
    assert CodeWebViewer._has_active_tool_dom(viewer) is True
