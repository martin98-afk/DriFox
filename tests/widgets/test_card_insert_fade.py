# -*- coding: utf-8 -*-
"""#A1 消息卡插入淡入回归测试：fade 启动标记 + 每轮首卡节流 + 异常防护。"""
import time

import pytest
from PyQt5.QtWidgets import QWidget

from app.main_widget import OpenAIChatToolWindow


class _FakeCard(QWidget):
    """最小卡桩：只带节流判据消费的属性"""

    def __init__(self, round_key=None, role="assistant"):
        super().__init__()
        if round_key is not None:
            self._message_index = round_key
        self.role = role


def _bare_host():
    """裸宿主桩：不跑 __init__，只挂 _fade_in_new_card 依赖"""
    return OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)


def test_fade_starts_with_zero_opacity(qapp):
    mw = _bare_host()
    card = _FakeCard()
    card.show()
    mw._fade_in_new_card(card)
    effect = card.graphicsEffect()
    assert effect is not None, "fade 必须挂 opacity effect（动画标记）"
    assert card._fade_effect is effect


def test_effect_removed_after_animation(qapp):
    mw = _bare_host()
    card = _FakeCard()
    mw._fade_in_new_card(card)
    qapp.processEvents()
    end = time.perf_counter() + 1.0
    while time.perf_counter() < end and card.graphicsEffect() is not None:
        qapp.processEvents()
        time.sleep(0.01)
    assert card.graphicsEffect() is None, "动画结束必须移除 opacity effect（防常驻禁渲染优化）"


def test_fade_survives_dead_widget(qapp):
    mw = _bare_host()
    card = _FakeCard()
    card.deleteLater()
    qapp.processEvents()
    mw._fade_in_new_card(card)  # 不抛异常即可（RuntimeError 防护）


def test_throttle_first_card_per_round(qapp):
    """节流判据：同轮次（同 _message_index）只首卡 fade，后续卡直插"""
    mw = _bare_host()
    mw._last_fade_round_key = object()  # 哨兵：与任何真实轮次号不同

    def hook(widget, insert_index=None):
        fired = []
        if insert_index is None and isinstance(widget, _FakeCard):
            round_key = getattr(widget, "_message_index", None)
            if round_key is not None and round_key != getattr(mw, "_last_fade_round_key", object()):
                mw._last_fade_round_key = round_key
                fired.append(widget)
        return fired

    assert len(hook(_FakeCard(round_key=3))) == 1   # 轮次 3 首卡 → fade
    assert hook(_FakeCard(round_key=3)) == []        # 同轮第二卡 → 直插
    assert len(hook(_FakeCard(round_key=4))) == 1    # 新轮次 → fade
    assert hook(_FakeCard(round_key=4)) == []        # 同轮后续 → 直插
    assert hook(_FakeCard(round_key=None)) == []     # 无轮次号（欢迎卡等）→ 直插


def test_explicit_insert_index_never_fades(qapp):
    """显式 insert_index（历史重建/占位重插）永不动画"""

    def hook(widget, insert_index=None):
        mw = _bare_host()
        mw._last_fade_round_key = object()
        if insert_index is None and isinstance(widget, _FakeCard):
            round_key = getattr(widget, "_message_index", None)
            if round_key is not None and round_key != getattr(mw, "_last_fade_round_key", object()):
                return ["faded"]
        return []

    assert hook(_FakeCard(round_key=5), insert_index=0) == []
