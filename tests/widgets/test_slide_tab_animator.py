# -*- coding: utf-8 -*-
"""SlideTabAnimator：QStackedWidget 两段式方向滑动（方向、摘除/归位、打断一致性）"""

import pytest
from PyQt5.QtWidgets import QLabel, QStackedWidget, QVBoxLayout, QWidget

from app.utils.design_tokens import Animations
from app.widgets.slide_tab_animator import SlideTabAnimator


@pytest.fixture(autouse=True)
def force_motion(monkeypatch):
    """屏蔽系统「减少动态效果」，保证动画路径确定性"""
    monkeypatch.setattr(Animations, "motion_enabled", classmethod(lambda cls: True))


@pytest.fixture
def anim(qtbot):
    host = QWidget()
    host.resize(400, 300)
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    stack = QStackedWidget(host)
    layout.addWidget(stack)
    for name in ("A", "B", "C"):
        stack.addWidget(QLabel(name, stack))
    stack.setCurrentIndex(0)
    qtbot.addWidget(host)
    host.show()
    qtbot.waitExposed(host)
    # 同时拼出 host 引用：只返回 anim/stack 时 host 会被 GC，连带 stack 一起销毁
    return SlideTabAnimator(stack, layout), stack, layout, host


def _wait_done(qtbot, a):
    qtbot.waitUntil(lambda: not a.is_running(), timeout=3000)


def test_switch_right_two_phase(qtbot, anim):
    """C 在右：段1 旧页画面向左滑出（此时未换页），段2 新页滑入归位"""
    a, stack, layout, _host = anim
    swapped = []
    assert a.switch(2, 1, on_switched=lambda: swapped.append(True)) is True
    assert a.is_running()
    assert layout.count() == 0  # 动画期间从布局摘除，避免布局拉回几何
    stack.move(-stack.width() // 2, 0)  # 模拟段1 中间帧
    assert stack.currentIndex() == 0  # 段1 未换页（旧画面仍在）
    assert not swapped
    _wait_done(qtbot, a)
    assert stack.currentIndex() == 2
    assert swapped
    assert layout.count() == 1  # 收尾放回布局
    assert stack.x() == 0  # 归位


def test_switch_left_direction(qtbot, anim):
    """A 在左：direction=-1 时旧页画面向右（出场侧）滑出"""
    a, stack, layout, _host = anim
    stack.setCurrentIndex(2)
    assert a.switch(0, -1) is True
    a._on_tick(0.5)
    assert stack.x() > 0  # 向右出画
    _wait_done(qtbot, a)
    assert stack.currentIndex() == 0
    assert stack.x() == 0


def test_interrupt_still_switches_page(qtbot, anim):
    """段1 被打断：换页仍完成并放回布局，杜绝高亮/内容状态漂移"""
    a, stack, layout, _host = anim
    swapped = []
    a.switch(1, 1, on_switched=lambda: swapped.append(True))
    a.finish_now()
    assert stack.currentIndex() == 1
    assert swapped
    assert not a.is_running()
    assert layout.count() == 1


def test_no_handoff_cases(qtbot, anim):
    """方向为 0 / 系统减少动态效果 / 越界：均不接手，调用方走直切"""
    a, stack, layout, _host = anim
    assert a.switch(1, 0) is False
    assert a.switch(1, 1) is True
    _wait_done(qtbot, a)
    assert a.switch(1, 1) is False  # 同页无操作
    assert a.switch(9, 1) is False  # 越界
    monkey = Animations.motion_enabled
    try:
        Animations.motion_enabled = classmethod(lambda cls: False)
        assert a.switch(0, -1) is False
    finally:
        Animations.motion_enabled = monkey
