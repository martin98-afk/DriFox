# -*- coding: utf-8 -*-
"""splitter 角点联动 + 延迟高亮：几何检测与双轴拖动

覆盖 ``app/widgets/splitter_corners.py`` 的核心行为：
- 横/纵 splitter 的 handle 在几何上交叉时能双向识别出角点；
- 角点拖动同时改变两个 splitter 的尺寸，且以拖动起点为基准（来回拖不漂移）；
- 窗格最小尺寸约束不被突破；
- handle 悬停延迟到期后置位 splitterHot 属性（样式表据此点亮）。
"""

import sys

from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QSplitter, QVBoxLayout, QWidget

_QAPP = QApplication.instance() or QApplication(sys.argv)

from app.widgets.splitter_corners import SplitterCornerController


def _build_window():
    """左窗格 | (上窗格 / 下窗格) 的嵌套布局：两个 splitter 方向正交"""
    window = QWidget()
    window.resize(600, 400)
    outer = QSplitter(Qt.Horizontal, window)
    left = QWidget()
    left.setMinimumWidth(60)
    vertical = QSplitter(Qt.Vertical, outer)
    vertical.setMinimumWidth(120)
    top = QWidget()
    top.setMinimumHeight(60)
    bottom = QWidget()
    bottom.setMinimumHeight(60)
    vertical.addWidget(top)
    vertical.addWidget(bottom)
    outer.addWidget(left)
    outer.addWidget(vertical)
    layout = QVBoxLayout(window)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(outer)
    window.show()
    QTest.qWait(80)
    outer.setSizes([300, 300])
    QTest.qWait(50)
    return window, outer, vertical


def _corner_pos(outer, vertical):
    """两条 handle 的交点（outer.handle(1) 与 vertical.handle(1) 相接处）"""
    handle = outer.handle(1)
    rect = handle.geometry()
    return handle.mapToGlobal(QPoint(rect.width() // 2, rect.height() // 2 + 0))


def test_corner_detected_from_both_handles():
    window, outer, vertical = _build_window()
    controller = SplitterCornerController()
    try:
        h_outer = outer.handle(1)
        h_vertical = vertical.handle(1)
        # 交点取 outer handle 的中间高度
        gpos = h_outer.mapToGlobal(QPoint(2, h_outer.height() // 2))
        # 纵 handle 的水平中线与交点一致
        gpos.setY(h_vertical.mapToGlobal(QPoint(0, h_vertical.height() // 2)).y())
        assert controller._corner_at(h_outer, gpos) is not None
        assert controller._corner_at(h_vertical, gpos) is not None
    finally:
        controller.stop()
        window.deleteLater()


def test_corner_drag_moves_both_splitters():
    window, outer, vertical = _build_window()
    controller = SplitterCornerController()
    try:
        h_outer = outer.handle(1)
        h_vertical = vertical.handle(1)
        gpos = QPoint(
            h_outer.mapToGlobal(QPoint(2, 0)).x(),
            h_vertical.mapToGlobal(QPoint(0, h_vertical.height() // 2)).y(),
        )
        before_outer = list(outer.sizes())
        before_vertical = list(vertical.sizes())
        assert controller._begin_drag(h_outer, gpos) is True
        controller._drag_to(gpos + QPoint(80, 60))
        assert outer.sizes()[0] == before_outer[0] + 80
        assert vertical.sizes()[0] == before_vertical[0] + 60
        # 回拖以起点为基准：不累积漂移
        controller._drag_to(gpos + QPoint(10, 10))
        assert outer.sizes()[0] == before_outer[0] + 10
        assert vertical.sizes()[0] == before_vertical[0] + 10
        controller._end_drag()
    finally:
        controller.stop()
        window.deleteLater()


def test_shift_pair_respects_minimum_sizes():
    window, outer, vertical = _build_window()
    controller = SplitterCornerController()
    try:
        sizes = list(outer.sizes())
        # 向左狠拖：左窗格不会小于 minimumWidth(60)
        controller._shift_pair(outer, 1, sizes, -100000)
        assert outer.sizes()[0] >= 60
        assert outer.sizes()[0] + outer.sizes()[1] == sizes[0] + sizes[1]
        # 向右狠拖：右窗格不会小于 minimumWidth(120)
        controller._shift_pair(outer, 1, sizes, 100000)
        assert outer.sizes()[1] >= 120
    finally:
        controller.stop()
        window.deleteLater()


def test_hover_hot_property_toggles():
    window, outer, vertical = _build_window()
    controller = SplitterCornerController()
    try:
        handle = outer.handle(1)
        assert bool(handle.property("splitterHot")) is False
        controller._set_hot(handle, True)
        assert bool(handle.property("splitterHot")) is True
        controller._on_leave(handle)
        # 未进过 pending 队列的 handle 由 _set_hot 直接熄灭
        controller._set_hot(handle, False)
        assert bool(handle.property("splitterHot")) is False
    finally:
        controller.stop()
        window.deleteLater()
