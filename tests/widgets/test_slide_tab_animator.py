# -*- coding: utf-8 -*-
"""SlideTabAnimator：QStackedWidget 两段式方向滑动（方向、摘除/归位、打断一致性）"""

import pytest
from PyQt5.QtCore import QVariantAnimation
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


def test_animation_objects_bounded(qtbot, anim):
    a, stack, _, _host = anim
    for target in (1, 2, 0, 1):
        assert a.switch(target, 1)
        _wait_done(qtbot, a)
    assert len(a.findChildren(QVariantAnimation)) <= 1


def test_reverse_before_swap(qtbot, anim):
    a, stack, layout, _host = anim
    assert a.switch(1, 1)
    if not a.switch(0, -1):
        stack.setCurrentIndex(0)  # 工作台的直切回退
    _wait_done(qtbot, a)
    assert stack.currentIndex() == 0
    assert layout.indexOf(stack) == 0


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


def test_reverse_mid_out_bounces_back(qtbot, anim):
    """段1 离场途中点回仍显示的旧页：倒放归位，不换页、不重建动画对象"""
    a, stack, layout, _host = anim
    swapped = []
    assert a.switch(2, 1, on_switched=lambda: swapped.append(True)) is True
    qtbot.waitUntil(lambda: stack.x() < -5, timeout=1500)
    assert stack.currentIndex() == 0  # 段1 未换页
    assert a.switch(0, -1) is True  # 接手回弹，调用方不再直切
    assert a._phase == "back"
    assert stack.currentIndex() == 0
    assert not swapped  # 全程未换页，段间回调不触发
    _wait_done(qtbot, a)
    assert stack.x() == 0
    assert layout.indexOf(stack) == 0
    assert not swapped
    assert len(a.findChildren(QVariantAnimation)) <= 1


def test_burst_reverse_last_request_wins(qtbot, anim):
    """段1/段2 中途连续反向：最终停在最后一次请求的页，布局归位"""
    a, stack, layout, _host = anim
    assert a.switch(2, 1)
    qtbot.waitUntil(lambda: stack.x() < -5, timeout=1500)
    assert a.switch(1, -1)  # 段1 中途反向到第三页
    qtbot.waitUntil(lambda: a._phase == "in", timeout=2000)
    assert stack.currentIndex() == 1
    assert a.switch(0, -1)  # 段2 中途反向到原始页
    _wait_done(qtbot, a)
    assert stack.currentIndex() == 0
    assert layout.indexOf(stack) == 0
    assert stack.x() == 0
    assert len(a.findChildren(QVariantAnimation)) <= 1


def test_reverse_mid_in_no_snap(qtbot, anim):
    """段2 滑入途中反向：倒放回画外再换方向进场，反向瞬间不归位跳变"""
    a, stack, layout, _host = anim
    assert a.switch(2, 1)
    qtbot.waitUntil(lambda: a._phase == "in", timeout=2000)
    qtbot.wait(60)  # 滑入中途（x 在画内、未归位）
    x_before = stack.x()
    assert 0 < x_before < stack.width()
    assert a.switch(0, -1) is True
    assert a._phase == "back_in"
    assert stack.x() == x_before  # 反向瞬间保持原位，不跳回 0
    assert stack.currentIndex() == 2  # 仍显示旧目标，换页发生在画外
    _wait_done(qtbot, a)
    assert stack.currentIndex() == 0
    assert layout.indexOf(stack) == 0
    assert stack.x() == 0


def test_reverse_back_then_new_target(qtbot, anim):
    """回弹途中改点新目标：回弹被取代，最终停在新目标且布局归位"""
    a, stack, layout, _host = anim
    assert a.switch(2, 1)
    qtbot.waitUntil(lambda: stack.x() < -5, timeout=1500)
    assert a.switch(0, -1) is True
    assert a._phase == "back"
    assert a.switch(1, 1) is True
    _wait_done(qtbot, a)
    assert stack.currentIndex() == 1
    assert layout.indexOf(stack) == 0
    assert stack.x() == 0


def test_rapid_burst_no_animation_growth(qtbot, anim):
    """连续打断不等待完成：动画对象恒定、最终布局归位"""
    a, stack, layout, _host = anim
    for target, direction in ((2, 1), (1, -1), (0, -1), (2, 1), (1, -1)):
        a.switch(target, direction)
        qtbot.wait(20)  # 制造中断，不等待动画完成
    _wait_done(qtbot, a)
    assert stack.currentIndex() == 1
    assert layout.count() == 1
    assert stack.x() == 0
    assert len(a.findChildren(QVariantAnimation)) <= 1
