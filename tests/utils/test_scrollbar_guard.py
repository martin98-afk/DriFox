# -*- coding: utf-8 -*-
"""事件类型混淆守卫单测（ScrollBar 类级补丁 + 应用级过滤器）。

背景：同一类「事件包装类型与 type() 字段错位」事故已二次复发。
- 2026-11-02：ScrollBar.eventFilter 取 e.size()，事件包装是 QWheelEvent
- 2026-11-05：ExpandBorderWidget.eventFilter 取 e.size()，包装是 QMouseEvent

真机形态是悬垂 wrapper（Python 持有事件后 C++ 对象销毁、内存被复用），
Python 侧无法原样构造，故分两条路验证：
- 类级补丁：纯 QEvent(QEvent.Resize) 的包装就是 QEvent，与真机错位对象
  同形（type==Resize + 非 QResizeEvent），可直接当替身。
- 应用级过滤器：判据是「包装为 QInputEvent 且 type==Resize」，用
  MagicMock(spec=QInputEvent) 指定包装类型来验证判据本身。
"""

from PySide6.QtCore import QEvent, QPoint, QPointF, QSize, Qt
from PySide6.QtGui import QMouseEvent, QResizeEvent, QWheelEvent
from PySide6.QtWidgets import QScrollArea, QWidget

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


# ───────── 应用级过滤器（2026-11-05 ExpandBorderWidget 事故新增） ─────────

def _confused(mock_cls=None):
    """造「包装是输入事件、type() 却返回 Resize」的错位事件替身。

    真机形态是悬垂 wrapper（Python 持有 QMouseEvent，C++ 对象销毁后内存
    被复用，wrapper 类型不变而 type() 读到 14），Python 侧无法原样构造，
    故用 MagicMock 指定 spec=QInputEvent 冒充包装类型，只验判据。
    """
    from unittest.mock import MagicMock

    from PySide6.QtGui import QInputEvent

    ev = MagicMock(spec=mock_cls or QInputEvent)
    ev.type.return_value = QEvent.Resize
    return ev


def test_app_guard_swallows_confused_input_event(qapp):
    """错位输入事件（包装 QInputEvent + type=Resize）→ 拦下"""
    from app.utils.scrollbar_guard import _ResizeTypeGuard

    guard = _ResizeTypeGuard(qapp)
    assert guard.eventFilter(None, _confused()) is True


def test_app_guard_passes_input_event_with_real_type(qapp):
    """正常鼠标事件（type 与包装一致）不被拦"""
    from app.utils.scrollbar_guard import _ResizeTypeGuard

    guard = _ResizeTypeGuard(qapp)
    ev = QMouseEvent(
        QEvent.MouseButtonPress, QPointF(5, 5), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier
    )
    assert guard.eventFilter(None, ev) is False


def test_app_guard_passes_real_resize(qapp):
    """真 QResizeEvent 照常下发"""
    from app.utils.scrollbar_guard import _ResizeTypeGuard

    guard = _ResizeTypeGuard(qapp)
    ev = QResizeEvent(QSize(200, 100), QSize(150, 80))
    assert guard.eventFilter(None, ev) is False


def test_app_guard_passes_plain_resize_substitute(qapp):
    """宿主/单测自建的纯 QEvent(Resize) 不被吞（判据只认输入事件包装）

    回归 tests/widgets/test_welcome_card_tab_bar.py::test_layout_change_reschedules_hover_sync
    """
    from app.utils.scrollbar_guard import _ResizeTypeGuard

    guard = _ResizeTypeGuard(qapp)
    assert guard.eventFilter(None, QEvent(QEvent.Resize)) is False


def test_app_guard_installed_and_held(qapp):
    """install_guard 挂上应用级过滤器并持引用（防 GC 后失效），且幂等"""
    scrollbar_guard.install_guard()
    first = getattr(qapp, "_drifox_resize_guard", None)
    assert first is not None
    scrollbar_guard.install_guard()
    assert qapp._drifox_resize_guard is first
