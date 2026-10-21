# -*- coding: utf-8 -*-
"""
TabManagerWindow 拖拽性能诊断测试

测量 moveEvent 的调用间隔，判断是否存在拖拽卡顿。
阈值：连续 moveEvent 间隔 > 33ms（≈30fps）视为卡顿。
"""

import time

import pytest
from PyQt5.QtCore import QPoint, Qt, QTimer
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from app.widgets.tab_manager_window import TabManagerWindow


@pytest.fixture(autouse=True)
def reset_singleton():
    """每个测试前后重置单例"""
    from app.tray_manager import TrayManager

    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    yield
    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None


def test_move_event_timing(qtbot):
    """测量 TabManagerWindow 拖拽时 moveEvent 的间隔"""
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm.show()
    qtbot.wait(100)  # 等待窗口显示

    # 记录每次 moveEvent 的时间戳
    timestamps = []

    original_move = tm.moveEvent

    def instrumented_move(event):
        timestamps.append(time.monotonic())
        original_move(event)

    tm.moveEvent = instrumented_move

    # 模拟快速连续移动（模拟拖拽）
    start_pos = tm.pos()
    for dx in range(0, 200, 10):
        tm.move(start_pos.x() + dx, start_pos.y())
        QApplication.processEvents()

    qtbot.wait(50)

    # 分析间隔
    if len(timestamps) < 2:
        pytest.skip("未能捕获足够 moveEvent")

    intervals = []
    for i in range(1, len(timestamps)):
        interval_ms = (timestamps[i] - timestamps[i - 1]) * 1000
        intervals.append(interval_ms)

    avg_interval = sum(intervals) / len(intervals)
    max_interval = max(intervals)
    over_33ms = sum(1 for iv in intervals if iv > 33)

    print(f"\n=== TabManagerWindow 拖拽性能诊断 ===")
    print(f"moveEvent 次数: {len(timestamps)}")
    print(f"平均间隔: {avg_interval:.1f}ms")
    print(f"最大间隔: {max_interval:.1f}ms")
    print(f"超过 33ms 的次数: {over_33ms}/{len(intervals)}")

    # 输出间隔直方图
    buckets = {"<8ms": 0, "8-16ms": 0, "16-33ms": 0, "33-50ms": 0, "50-100ms": 0, ">100ms": 0}
    for iv in intervals:
        if iv < 8:
            buckets["<8ms"] += 1
        elif iv < 16:
            buckets["8-16ms"] += 1
        elif iv < 33:
            buckets["16-33ms"] += 1
        elif iv < 50:
            buckets["33-50ms"] += 1
        elif iv < 100:
            buckets["50-100ms"] += 1
        else:
            buckets[">100ms"] += 1
    for k, v in buckets.items():
        bar = "█" * min(v, 40)
        print(f"  {k}: {v} {bar}")

    # 如果有超过 33ms，说明存在卡顿
    if over_33ms > 0:
        print("⚠️ 检测到拖拽卡顿（间隔 > 33ms）")


def _measure_move_perf(widget, label="", steps=50):
    """测量一组 programmatic move 的总耗时和帧间隔"""
    timestamps = []
    original_move = widget.moveEvent

    def instrumented(event):
        timestamps.append(time.monotonic())
        original_move(event)

    widget.moveEvent = instrumented

    start = time.monotonic()
    base = widget.pos()
    for i in range(steps):
        widget.move(base.x() + i * 3, base.y())
        QApplication.processEvents()
    elapsed = (time.monotonic() - start) * 1000  # ms

    widget.moveEvent = original_move  # 恢复

    if len(timestamps) >= 2:
        intervals = [(timestamps[i] - timestamps[i - 1]) * 1000 for i in range(1, len(timestamps))]
        avg = sum(intervals) / len(intervals)
        mx = max(intervals)
        over_33 = sum(1 for iv in intervals if iv > 33)
    else:
        intervals = []
        avg = mx = over_33 = 0

    print(f"  [{label}] {steps}次move总耗时: {elapsed:.0f}ms | "
          f"平均帧间隔: {avg:.1f}ms | 最大: {mx:.1f}ms | >33ms: {over_33}")
    return avg, mx, over_33


def test_window_stays_on_top_with(qtbot):
    """带 WindowStaysOnTopHint 的拖拽性能基线"""
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm.show()
    qtbot.wait(200)
    _measure_move_perf(tm, label="WITH StaysOnTopHint", steps=50)


def test_window_stays_on_top_without(qtbot):
    """不带 WindowStaysOnTopHint 的拖拽性能基线（用独立窗口，避免 setWindowFlags 销毁对象）"""
    # 创建两个独立实例来对比，不修改已有窗口的标志
    # 直接从 QWidget 创建，模拟移除 StaysOnTopHint 的行为
    from PyQt5.QtWidgets import QWidget, QHBoxLayout
    from PyQt5.QtCore import Qt as QtCore

    tm = QWidget()
    tm.setObjectName("tabManagerWindow")
    tm.setWindowTitle("飘狐-DriFox PerfTest")
    tm.setMinimumSize(500, 400)
    # 移除 WindowStaysOnTopHint
    tm.setWindowFlags(
        QtCore.Window
        | QtCore.WindowTitleHint
        | QtCore.WindowSystemMenuHint
        | QtCore.WindowMinimizeButtonHint
        | QtCore.WindowMaximizeButtonHint
        | QtCore.WindowCloseButtonHint
    )
    # 简易布局
    layout = QHBoxLayout(tm)
    from app.widgets.tab_panel import TabPanel
    panel = TabPanel(tm)
    layout.addWidget(panel)

    qtbot.addWidget(tm)
    tm.show()
    qtbot.wait(200)
    _measure_move_perf(tm, label="WITHOUT StaysOnTopHint", steps=50)
