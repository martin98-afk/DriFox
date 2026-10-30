# -*- coding: utf-8 -*-
"""回归测试：v2 通道下工具运行框纳入块系统（不再走 JS 注入裸节点）。

背景（2026-10-31 用户反馈）：工具运行框出现后到完成框之间会「消失一阵」，
且运行框位置会在思考块上下跳动。根因是两套节点体系混排：运行框是 JS 注入的
裸节点（非 data-block-wrapper），完成框是 patchBlocks 管理的块节点；为共存堆了
每批去重删除、插入位置绕开裸节点、dirty 保护等补丁。

修复：运行框写成 ``tool_streaming`` 块进 ``_content_data``，由块级 diff 管位置
与内容；``append_tool_result`` 把同 id 的 streaming 块换成 ``tool_result`` 块，
一次替换同帧完成；v2 下不再执行任何 JS 注入。

本测试覆盖 Python 侧不变量（viewer 用无头桩）：
  1. ``update_tool_streaming`` → 写入/原位替换 ``tool_streaming`` 块，零 JS 调用；
  2. 同 id 多次更新 → 块数量不增，preview/进度就地更新；
  3. ``append_tool_result`` → streaming 块消失、tool_result 块出现，仍零 JS 注入；
  4. ``remove_tool_streaming`` → 块被移除。
"""

import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


class _StubViewer:
    """v2 通道 viewer 桩：记录调度与 JS 调用，不碰 WebEngine。"""

    def __init__(self):
        self._render_v2 = True
        self._streaming = True
        self._restore_finished_ids = None
        self._schedule_render_calls = []
        self.js_calls = []
        self._tool_target_id = "tool-content"
        self._tool_compact_mode = True
        self._lazy_markdown_cb = None

    def _schedule_render(self, immediate=False):
        self._schedule_render_calls.append(bool(immediate))

    def page(self):
        return self

    def runJavaScript(self, js_code):
        self.js_calls.append(js_code)


def _make_card():
    from app.widgets.message_card import MessageCard

    _ensure_qapp()
    card = MessageCard(role="assistant")
    card._lazy_rendered = True
    card.viewer = _StubViewer()
    return card


def _types(card):
    return [b.get("type") for b in card._content_data if isinstance(b, dict)]


def _streaming_blocks(card):
    return [
        b
        for b in card._content_data
        if isinstance(b, dict) and b.get("type") == "tool_streaming"
    ]


def test_update_tool_streaming_writes_block_without_js():
    card = _make_card()
    card.update_tool_streaming("call_1", "bash", {"command": "echo hi"})

    blocks = _streaming_blocks(card)
    assert len(blocks) == 1, f"应写入 1 个 tool_streaming 块，实际 {_types(card)}"
    block = blocks[0]
    assert block["tool_call_id"] == "call_1"
    assert block["name"] == "bash"
    assert block["completed"] is False
    assert card.viewer.js_calls == [], "v2 下运行框不得再走 JS 注入"
    assert card.viewer._schedule_render_calls, "首次出现应触发渲染调度"


def test_repeated_updates_replace_in_place():
    card = _make_card()
    card.update_tool_streaming("call_1", "bash", {"command": "echo"})
    card.update_tool_streaming("call_1", "bash", {"command": "echo hi"})
    card.update_tool_streaming("call_1", "bash", {"command": "echo hi", "timeout": 30})

    blocks = _streaming_blocks(card)
    assert len(blocks) == 1, f"同 id 更新不得累积副本：{_types(card)}"
    assert "命令" in blocks[0]["preview"] or blocks[0]["preview"], "预览文本应就位"
    assert card.viewer.js_calls == []


def test_finish_tool_streaming_marks_completed():
    card = _make_card()
    card.update_tool_streaming("call_1", "bash", {"command": "echo hi"})
    card.finish_tool_streaming("call_1", "bash", {"command": "echo hi"})

    blocks = _streaming_blocks(card)
    assert len(blocks) == 1
    assert blocks[0]["completed"] is True
    assert card.viewer.js_calls == []


def test_append_tool_result_replaces_streaming_block_without_js():
    card = _make_card()
    card.update_tool_streaming("call_1", "bash", {"command": "echo hi"})
    card.finish_tool_streaming("call_1", "bash", {"command": "echo hi"})

    card.append_tool_result(
        tool_name="bash",
        arguments={"command": "echo hi"},
        result="hi\n",
        success=True,
        tool_call_id="call_1",
    )

    assert _streaming_blocks(card) == [], "完成后不得残留 tool_streaming 块（否则重复/顺序错乱）"
    assert "tool_result" in _types(card), f"应写入 tool_result 块：{_types(card)}"
    assert card.viewer.js_calls == [], "v2 下完成框由补丁渲染，不得注入 JS"
    assert "call_1" in card._finished_streaming_ids


def test_remove_tool_streaming_drops_block():
    card = _make_card()
    card.update_tool_streaming("call_1", "bash", {"command": "echo hi"})
    assert len(_streaming_blocks(card)) == 1

    card.remove_tool_streaming("call_1")
    assert _streaming_blocks(card) == [], "移除后不得残留运行框块"
    assert card.viewer.js_calls == []
