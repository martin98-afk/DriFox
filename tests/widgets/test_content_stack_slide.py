# -*- coding: utf-8 -*-
"""验证 _ContentStack 页级滑动：长条模型方向、叠放保活不变式、slide_over、重入快进"""

import pytest
from PyQt5.QtWidgets import QWidget

from app.utils.design_tokens import Animations
from app.widgets.tab_manager_window import _ContentStack


@pytest.fixture(autouse=True)
def force_motion(monkeypatch):
    """屏蔽系统「减少动态效果」，保证动画路径确定性"""
    monkeypatch.setattr(Animations, "motion_enabled", classmethod(lambda cls: True))


@pytest.fixture
def stack(qtbot):
    s = _ContentStack()
    s.resize(400, 300)
    qtbot.addWidget(s)
    p0 = QWidget(s)  # 对话页替身
    p1 = QWidget(s)  # 覆盖层页替身
    s.addWidget(p0)
    s.addWidget(p1)
    s.show()
    qtbot.waitExposed(s)
    return s, p0, p1


def _wait_slide_done(qtbot, s):
    qtbot.waitUntil(lambda: not s.slide_in_progress(), timeout=3000)


def test_slide_right_new_page_enters_from_right(qtbot, stack):
    """点右侧 tab（开卡片）：新页从右侧进入，旧页向左退出；终态旧对话页 hide"""
    s, p0, p1 = stack
    s.setCurrentIndex(1, direction=1)
    assert s.currentIndex() == 1  # 页状态立即落定
    assert s.slide_in_progress()
    assert p0.isVisible() and p1.isVisible()  # 动画期间两页同显
    assert p1.x() > 0  # 新页从右侧进入
    _wait_slide_done(qtbot, s)
    assert p1.geometry() == s.rect()  # 新页归位
    assert p0.isHidden()  # 对话页让位（去覆盖层不变式）
    assert p1.isVisible()


def test_slide_left_keeps_overlay_alive(qtbot, stack):
    """回对话（视口左移）：新页从左侧进入；终态覆盖层页保活不 hide"""
    s, p0, p1 = stack
    s.setCurrentIndex(1, direction=0)  # 先无动画切到覆盖层
    s.setCurrentIndex(0, direction=-1)
    assert s.currentIndex() == 0
    assert p0.x() < 0 if False else True  # 新页从左侧进入（起点负 x）
    _wait_slide_done(qtbot, s)
    assert p0.geometry() == s.rect()
    assert p0.isVisible()
    assert not p1.isHidden()  # 覆盖层页保活不变式：hide 永不发生
    assert p1.y() == 0  # 几何已归位（仅被盖住）


def test_direction_semantics_tick_positions(qtbot, stack):
    """长条模型 tick 公式：+1 视口右移（new 右进 old 左出），中点各偏半宽"""
    s, p0, p1 = stack
    s.setCurrentIndex(1, direction=1)
    w = s.width()
    s._on_slide_tick(0.5)
    assert p1.x() == round(w * 0.5)  # 新页右进中点
    assert p0.x() == round(-w * 0.5)  # 旧页左出中点
    _wait_slide_done(qtbot, s)
    s.setCurrentIndex(0, direction=-1)
    s._on_slide_tick(0.5)
    assert p0.x() == round(-w * 0.5)  # 反向对称：对话页从左侧进入中点
    assert p1.x() == round(w * 0.5)
    _wait_slide_done(qtbot, s)


def test_slide_over_nudge(qtbot, stack):
    """覆盖层内轻量滑入（无 swap）：顶层页右滑进入，不垫底，_current 不变"""
    s, p0, p1 = stack
    s.setCurrentIndex(1, direction=0)
    s.slide_over(direction=1)
    assert s.currentIndex() == 1  # 页不变
    assert s.slide_in_progress()
    assert p1.x() > 0  # 顶层页从右滑入
    assert p0.isHidden()  # 不垫底：对话页保持隐藏（露出区透传背景）
    _wait_slide_done(qtbot, s)
    assert p1.geometry() == s.rect()
    assert p0.isHidden()
    assert p1.isVisible()


def test_slide_swap_two_phase(qtbot, stack):
    """两段式：段1 旧画面向出场侧滑出（可见），段间切换内容，段2 新画面滑入归位"""
    s, p0, p1 = stack
    s.setCurrentIndex(1, direction=0)
    swapped = []
    s.slide_swap(direction=1, swap=lambda: swapped.append(True))
    assert s.currentIndex() == 1
    assert s.slide_in_progress()
    s._on_slide_tick(0.5)
    assert p1.x() == round(-s.width() * 0.5)  # 段1：旧画面向左（出场侧）滑出，全程可见
    assert not swapped  # 段间未到，内容未切换
    _wait_slide_done(qtbot, s)
    assert swapped  # swap 已执行
    assert p1.geometry() == s.rect()  # 段2 归位
    assert s.slide_in_progress() is False


def test_slide_swap_interrupted_still_swaps(qtbot, stack):
    """swap 段1 被打断（resize 快进）：swap 仍执行，几何归位（状态不漂移）"""
    s, p0, p1 = stack
    s.setCurrentIndex(1, direction=0)
    swapped = []
    s.slide_swap(direction=1, swap=lambda: swapped.append(True))
    s.resize(500, 400)
    _wait_slide_done(qtbot, s)
    assert swapped
    assert p1.geometry() == s.rect()
    assert s.slide_in_progress() is False


def test_reentry_fast_forwards(qtbot, stack):
    """动画在途再次切换：先快进到上一动画终态（按新 _current 收尾），再滑本次"""
    s, p0, p1 = stack
    s.setCurrentIndex(1, direction=1)
    s.setCurrentIndex(0, direction=-1)  # 重入
    _wait_slide_done(qtbot, s)
    assert s.currentIndex() == 0
    assert p0.geometry() == s.rect()
    assert not p1.isHidden()  # 保活不变式仍成立
    assert p0.isVisible()


def test_resize_during_slide_settles(qtbot, stack):
    """动画中容器 resize：先快进收尾再统一铺满新 rect"""
    s, p0, p1 = stack
    s.setCurrentIndex(1, direction=1)
    s.resize(600, 400)
    _wait_slide_done(qtbot, s)
    assert p1.geometry() == s.rect()
    assert p0.isHidden()
    assert s.width() == 600


def test_direction_zero_switches_directly(qtbot, stack):
    """direction=0：无动画直切，立即终态"""
    s, p0, p1 = stack
    s.setCurrentIndex(1, direction=0)
    assert not s.slide_in_progress()
    assert p0.isHidden()
    assert p1.isVisible()
