# -*- coding: utf-8 -*-
"""ScrollBar 事件类型混淆守卫单测。

背景：真机出现「C++ 动态类型 QWheelEvent + type 字段=Resize」的错位事件，
qfluentwidgets ScrollBar.eventFilter 在 e.size() 上崩（2026-11-02）。
PyQt5/sip 的包装选择只看动态类型：纯 QEvent(QEvent.Resize) 的包装就是
QEvent（非 QResizeEvent），恰好可作混淆事件的测试替身，守卫判定路径与
真机一致（type==Resize + 非 QResizeEvent 包装）。
"""

from PyQt5.QtCore import QEvent, QPoint, QPointF, QSize, Qt
from PyQt5.QtGui import QResizeEvent, QWheelEvent
from PyQt5.QtWidgets import QScrollArea, QWidget

from app.utils import scrollbar_guard


def _wheel(delta=-120):
    pos = QPointF(1, 1)
    return QWheelEvent(
        pos, pos, QPoint(0, 0), QPoint(0, delta),
        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False,
    )


def _make_bar(qapp):
    from qfluentwidgets.components.widgets.scroll_bar import SmoothScrollBar

    area = QScrollArea()
    area.setWidget(QWidget())
    area.resize(200, 100)
    bar = SmoothScrollBar(Qt.Vertical, area)
    return area, bar


def test_confused_resize_event_is_released_without_crash(qapp):
    """type=Resize 但包装非 QResizeEvent → 放行且不触 _adjustPos（守卫命中）"""
    scrollbar_guard.install_guard()
    area, bar = _make_bar(qapp)
    calls = []
    orig = bar._adjustPos
    bar._adjustPos = lambda size: calls.append(size)

    ret = bar.eventFilter(area, QEvent(QEvent.Resize))
    assert ret is False
    assert calls == []  # 守卫拦下，没喂 e.size()


def test_real_resize_event_still_adjusts(qapp):
    """真 QResizeEvent 行为不变：照常 _adjustPos（一次）"""
    scrollbar_guard.install_guard()
    area, bar = _make_bar(qapp)
    calls = []
    bar._adjustPos = lambda size: calls.append(size)

    ev = QResizeEvent(QSize(200, 100), QSize(150, 80))
    bar.eventFilter(area, ev)
    assert len(calls) == 1


def test_wheel_event_unaffected(qapp):
    """普通滚轮事件不进守卫分支，原逻辑照跑（不抛异常即通过）"""
    scrollbar_guard.install_guard()
    area, bar = _make_bar(qapp)
    assert bar.eventFilter(area, _wheel()) is False


def test_delegate_confused_wheel_released(qapp):
    """SmoothScrollDelegate：type=Wheel 但包装非 QWheelEvent → 放行不喂 angleDelta"""
    from qfluentwidgets.components.widgets.scroll_bar import SmoothScrollDelegate

    scrollbar_guard.install_guard()
    area = QScrollArea()
    area.setWidget(QWidget())
    delegate = SmoothScrollDelegate(area)
    # 混淆对象：type 称 Wheel 但包装是纯 QEvent
    ret = delegate.eventFilter(area.viewport(), QEvent(QEvent.Wheel))
    assert ret is False


def test_install_guard_idempotent(qapp):
    from qfluentwidgets.components.widgets.scroll_bar import ScrollBar

    scrollbar_guard.install_guard()
    first = ScrollBar.eventFilter
    scrollbar_guard.install_guard()
    assert ScrollBar.eventFilter is first
