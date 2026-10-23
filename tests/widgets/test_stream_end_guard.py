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


# ── finish_streaming 幂等守卫（「结束态正文全丢」回归）──────────────
# 根因：_set_ai_state("idle") 扫尾兜底与 _on_stream_finished 正路对同一张卡
# 连收两次；第二遍递增 _render_seq 作废第一遍终渲染 + 二次清空 _markdown_text
# → 正文全丢。守卫用轮次标记（_finish_streamed_for_round）：只拦同轮重复
# finish，打断路径（stop 后 finish 强制归位）与历史加载不受影响。


class _RecordViewer:
    """记录 finish_streaming 调用的最小 stub。"""

    def __init__(self):
        self.finish_calls = []

    def finish_streaming(self, keep_dock=False, immediate=True):
        self.finish_calls.append((keep_dock, immediate))


def _fresh_card(qapp) -> MessageCard:
    card = MessageCard(role="assistant")
    card.viewer = _RecordViewer()
    return card


def test_double_finish_finalizes_once(qapp):
    """同轮第二次 finish 被拦（兜底 + 正路双收 → 只收一次，正文不再被清）。"""
    card = _fresh_card(qapp)
    card.start_streaming_anim()
    card.finish_streaming(force_dock_off=True)  # 兜底层先收
    n_after_first = len(card.viewer.finish_calls)
    card.finish_streaming()  # 正路第二遍
    assert len(card.viewer.finish_calls) == n_after_first == 1


def test_interruption_stop_then_finish_still_finalizes(qapp):
    """打断路径 stop → finish(force_dock_off) 必须照常归位（不被幂等误杀）。"""
    card = _fresh_card(qapp)
    card.start_streaming_anim()
    card.stop_streaming_anim()  # 打断：_streaming 已置 False
    card.finish_streaming(force_dock_off=True)
    assert len(card.viewer.finish_calls) == 1


def test_new_round_rearms_finish(qapp):
    """start_streaming_anim 开新一轮 → 幂等标记重置，可重新 finish。"""
    card = _fresh_card(qapp)
    card.start_streaming_anim()
    card.finish_streaming()
    card.start_streaming_anim()  # 新一轮
    card.finish_streaming()
    assert len(card.viewer.finish_calls) == 2


def test_history_finish_bypasses_guard(qapp):
    """history=True（历史加载收尾）恒放行。"""
    card = _fresh_card(qapp)
    card.finish_streaming(history=True)
    card.finish_streaming(history=True)
    assert len(card.viewer.finish_calls) == 2
