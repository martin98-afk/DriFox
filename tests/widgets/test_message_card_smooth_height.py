# -*- coding: utf-8 -*-
"""回归测试：流式直跳路径的限幅分帧（WebEngine 旧帧竖向拉伸伪影规避）。

背景：Qt6 下 widget 高度一次大跳（burst 内容更新 / 首帧撑开）时，Qt 侧在
Chromium 新帧到达前把旧帧纹理拉伸填满新几何，文字瞬间竖向拉长一闪
（ffmpeg 逐帧实测：行厚放大 ~3.7 倍、持续 ~130ms）。`_apply_height_smooth`
把单帧几何跳变限幅分帧（每帧 ≤160px），将每次拉伸比例压到视觉阈值以下。

验证：桩 viewer 驱动 `_apply_height_smooth`，断言步进幅度、追赶、过冲钳制、
delta/去重字段维护与 heightChanged 逐帧派发。
"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
_APP = QApplication.instance() or QApplication(sys.argv)

from app.widgets.message_card import MessageCard  # noqa: E402


class _HeightStubViewer:
    """高度桩：记录 setFixedHeight 序列，height() 返回最近值。"""

    def __init__(self, h=120):
        self._h = h
        self.calls = [h]

    def height(self):
        return self._h

    def setFixedHeight(self, h):
        self._h = int(h)
        self.calls.append(self._h)


def _make_card(viewer):
    card = MessageCard(role="assistant")
    card.viewer = viewer
    card._last_applied_viewer_height = viewer._h
    card._last_height_delta = 0
    card._smooth_height_timer = None
    card._smooth_height_target = None
    emits = []
    card.heightChanged.connect(emits.append)
    card._emits = emits
    return card


def test_small_jump_applies_immediately():
    """≤160px 的小跳不分帧，一步到位。"""
    card = _make_card(_HeightStubViewer(120))
    card._apply_height_smooth(240)
    assert card.viewer.calls == [120, 240]
    assert card._last_applied_viewer_height == 240
    assert card._last_height_delta == 120
    assert card._smooth_height_target is None, "到位后不应残留分帧目标"


def test_large_jump_steps_within_limit_and_converges():
    """大跳分帧：每步幅度不超限、最终收敛、delta/去重字段逐步维护。"""
    card = _make_card(_HeightStubViewer(120))
    card._apply_height_smooth(900)
    guard = 0
    # 模拟逐帧 fire：真实 timer 16ms 一次，测试中手动驱动直至收敛
    while card.viewer.height() != 900:
        card._apply_height_smooth(900)
        guard += 1
        assert guard < 20, "步进未收敛"
    steps = card.viewer.calls[1:]
    assert len(steps) > 1, "大跳必须分多帧"
    prev = 120
    for v in steps:
        assert abs(v - prev) <= MessageCard._SMOOTH_STEP_PX, f"单帧跳变超限: {prev}->{v}"
        prev = v
    assert card._last_applied_viewer_height == 900
    assert len(card._emits) == len(steps), "每帧都必须 emit heightChanged 供外层补偿"
    assert card._smooth_height_target is None


def test_new_target_supersedes_pending_sequence():
    """分帧途中新目标到来：以新目标继续逼近（追赶语义）。"""
    card = _make_card(_HeightStubViewer(120))
    card._apply_height_smooth(900)
    mid = card.viewer.height()
    card._apply_height_smooth(300)  # 流式期间目标被刷新
    guard = 0
    while card.viewer.height() != 300:
        card._apply_height_smooth(300)
        guard += 1
        assert guard < 20, "步进未收敛"
    assert card.viewer.calls[-1] == 300
    assert mid in card.viewer.calls


def test_collapse_direction_also_limited():
    """反向收缩（900→120）同样限幅，不得过冲。"""
    card = _make_card(_HeightStubViewer(900))
    card._apply_height_smooth(120)
    guard = 0
    while card.viewer.height() != 120:
        card._apply_height_smooth(120)
        guard += 1
        assert guard < 20
    assert card.viewer.calls[0] == 900
    for v in card.viewer.calls[1:]:
        assert v >= 120, "收缩方向不得过冲到目标以下"
    assert card._smooth_height_target is None
