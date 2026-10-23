# -*- coding: utf-8 -*-
"""回归：结束收尾目标解析 —— 引用失效时按状态回退找流式卡

历史（2026-10-18 → 2026-10-23 演进）：
- 2026-10-18 真机实证结束态残留（卡片永远停在 _streaming=True），当时以
  6s 自愈闸门 _guard_stream_end 兜底。
- 2026-10-23 发现闸门在思考期误触发（reasoning 静默累积不进 _markdown_text，
  思考 >6s 即被误判死流，每 6s 强制终态渲染 = 卡片高度突变再恢复），闸门
  已移除；真根因改为「finish 到达时 _current_assistant_card 已 deleted
  （流式中卡片被回收/重建，引用未转移）→ 收尾整体跳过」。
- 修复：_on_stream_finished 引用失效时按状态回退
  （find_last_streaming_assistant_card），收尾目标语义 = 「正在流式的那张
  卡」，与 Python 侧持着哪个引用无关。配套 cleanup 哨兵定位删除者。

本文件测回退查找语义（QVBoxLayout + 真实 MessageCard，不依赖 WebEngine）。
"""

import pytest

from app.widgets.message_card import MessageCard
from app.widgets.ui_helpers import find_last_streaming_assistant_card


def _make_card(role: str, streaming: bool) -> MessageCard:
    card = MessageCard(role=role)
    card._streaming = streaming
    return card


@pytest.fixture
def layout(qapp):
    from PySide6.QtWidgets import QVBoxLayout

    lay = QVBoxLayout()
    yield lay


def _add(layout, card):
    layout.addWidget(card)


def test_fallback_finds_last_streaming_card(layout):
    """多张卡中取最后一张流式卡（新列表重建后真正在流式的那张）。"""
    _add(layout, _make_card("assistant", streaming=False))
    _add(layout, _make_card("user", streaming=False))
    target = _make_card("assistant", streaming=True)
    _add(layout, target)
    assert find_last_streaming_assistant_card(layout) is target


def test_fallback_returns_none_when_all_settled(layout):
    """全部已收尾 → None（_on_stream_finished 侧不收尾，行为与旧逻辑一致）。"""
    _add(layout, _make_card("assistant", streaming=False))
    _add(layout, _make_card("user", streaming=False))
    assert find_last_streaming_assistant_card(layout) is None


def test_fallback_ignores_user_and_welcome_cards(layout):
    """user 卡与欢迎卡不参与回退（哪怕带 _streaming=True 脏状态）。"""
    _add(layout, _make_card("welcome", streaming=True))
    _add(layout, _make_card("user", streaming=True))
    assert find_last_streaming_assistant_card(layout) is None


def test_fallback_picks_latest_among_multiple_streaming(layout):
    """多张流式卡并存（插话场景）时取最后一张（最新一轮）。"""
    _add(layout, _make_card("assistant", streaming=True))
    target = _make_card("assistant", streaming=True)
    _add(layout, target)
    assert find_last_streaming_assistant_card(layout) is target


def test_fallback_empty_layout(layout):
    """空布局 → None。"""
    assert find_last_streaming_assistant_card(layout) is None
