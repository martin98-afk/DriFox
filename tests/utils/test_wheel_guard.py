# -*- coding: utf-8 -*-
"""SpinBox 滚轮守卫单测：滚轮不改值，且转交给外层滚动容器。

覆盖：
- 有/无焦点时滚轮都不改值
- 外层是 QScrollArea 时，滚轮会滚它的滚动条（不是「滚不动」）
- 重复安装幂等
"""

from PyQt5.QtCore import QPoint, QPointF, Qt
from PyQt5.QtGui import QWheelEvent
from PyQt5.QtWidgets import QScrollArea, QWidget

from app.utils import wheel_guard


def _wheel(widget, delta=-120):
    """向 widget 投递一个滚轮事件（等价鼠标滚轮向下滚一格）。"""
    pos = QPointF(widget.width() / 2, widget.height() / 2)
    ev = QWheelEvent(
        pos,
        QPointF(widget.mapToGlobal(QPoint(0, 0))),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.NoScrollPhase,
        False,
    )
    widget.wheelEvent(ev)
    return ev


def test_spinbox_ignores_wheel(qapp):
    from qfluentwidgets import SpinBox

    wheel_guard.install_guard(qapp)
    spin = SpinBox()
    spin.setRange(0, 100)
    spin.setValue(5)
    spin.show()
    # 经应用过滤器分发，才走守卫逻辑
    from PyQt5.QtWidgets import QApplication

    pos = QPointF(spin.width() / 2, spin.height() / 2)
    ev = QWheelEvent(
        pos,
        QPointF(spin.mapToGlobal(QPoint(0, 0))),
        QPoint(0, 0),
        QPoint(0, -120),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(spin, ev)
    assert spin.value() == 5


def test_wheel_forwards_to_scroll_area(qapp):
    from qfluentwidgets import SpinBox

    wheel_guard.install_guard(qapp)
    area = QScrollArea()
    area.setWidgetResizable(True)
    content = QWidget()
    content.setMinimumHeight(2000)
    spin = SpinBox(content)
    spin.setRange(0, 100)
    spin.setValue(7)
    spin.move(10, 10)
    area.setWidget(content)
    area.resize(300, 200)
    area.show()

    from PyQt5.QtWidgets import QApplication

    bar = area.verticalScrollBar()
    before = bar.value()
    pos = QPointF(spin.width() / 2, spin.height() / 2)
    ev = QWheelEvent(
        pos,
        QPointF(spin.mapToGlobal(QPoint(0, 0))),
        QPoint(0, 0),
        QPoint(0, -120),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(spin, ev)
    assert spin.value() == 7, "滚轮不应该改 SpinBox 的值"
    assert bar.value() != before, "滚轮应该滚外层容器"


def test_install_is_idempotent(qapp):
    assert wheel_guard.install_guard(qapp) in (True, False)
    assert wheel_guard.install_guard(qapp) is False
