# -*- coding: utf-8 -*-
"""paint_profiler 行为测试。

覆盖四条关键约束（都是实现时踩过或容易踩的坑）：
1. **源码仓默认开启、打包版默认关闭**：卡顿现场无法复现时，源码运行的
   日志里必须已经有数据；打包版不做常驻观测（`DRIFOX_PAINT_PROFILE=1`
   可强开）。环境变量同时用于关闭（`=0`）与调窗口。
2. 过滤器能按类统计 Paint 事件：Qt 的 Paint 派发必经
   ``QApplication`` 事件过滤器，这是唯一有效的统计点；给
   ``QWidget.paintEvent`` 打 Python 补丁是无效的（C++ 虚函数调度，
   计数恒为 0），本测试用真实绘制路径锁住正确实现。
3. 过滤器绝不吞事件：``eventFilter`` 必须返回 False，否则控件不再重绘。
4. 周期滚动采样：``reset()`` 用 ``clear()`` 原地清空，否则过滤器闭包
   会写进废弃字典，第二轮起统计恒为空。
"""

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from app.utils import paint_profiler  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """每个用例前后都复位：全局状态与过滤器不能跨用例残留。"""
    monkeypatch.delenv(paint_profiler._ENV_KEY, raising=False)
    paint_profiler.stop()
    paint_profiler.reset()
    yield
    paint_profiler.stop()
    paint_profiler.reset()


def test_enabled_by_default(qapp):
    """没有环境变量时默认启用（用户零配合是硬要求）。"""
    assert paint_profiler.enabled() is True
    assert paint_profiler.start() is True
    assert paint_profiler._started is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_disabled_values(qapp, monkeypatch, value):
    monkeypatch.setenv(paint_profiler._ENV_KEY, value)
    assert paint_profiler.enabled() is False


@pytest.mark.parametrize("value", ["", "1", "true", "yes", "on"])
def test_enabled_values(monkeypatch, value):
    monkeypatch.setenv(paint_profiler._ENV_KEY, value)
    assert paint_profiler.enabled() is True

def test_packaged_disabled_by_default(monkeypatch):
    """打包版默认关闭：诊断设施不随产品发布。"""
    monkeypatch.delenv(paint_profiler._ENV_KEY, raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert paint_profiler.enabled() is False

def test_packaged_forced_on(monkeypatch):
    """打包版排障时 DRIFOX_PAINT_PROFILE=1 强开。"""
    monkeypatch.setenv(paint_profiler._ENV_KEY, "1")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert paint_profiler.enabled() is True


def test_counts_paint_events_by_class(qapp, monkeypatch):
    """真实绘制路径下按类名累计次数（≥ 触发次数，Qt 可能合并 update）。"""
    monkeypatch.setenv(paint_profiler._ENV_KEY, "1")
    assert paint_profiler.start(window_s=5) is True

    class _Probe(QWidget):
        def paintEvent(self, e):  # noqa: N802 - Qt 命名
            p = QPainter(self)
            p.fillRect(self.rect(), QColor("#345"))
            p.end()

    w = _Probe()
    w.resize(120, 90)
    w.show()

    # 用 update() + 事件循环走真实 Paint 路径（repaint() 走 C++ 直绘，
    # 不经过 QApplication 过滤器，会产生假阴性）
    state = {"n": 0, "done": False}

    def tick():
        state["n"] += 1
        w.update()
        if state["n"] >= 12:
            timer.stop()
            state["done"] = True
            loop.quit()

    from PySide6.QtCore import QEventLoop

    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(15)
    QTimer.singleShot(8000, loop.quit)
    loop.exec()

    assert state["done"], "事件循环未按预期推进"
    assert paint_profiler._stats.get("_Probe", 0) >= 1, (
        "Paint 事件未被统计到 —— 若为 0，说明统计点用错了"
        "（给 QWidget.paintEvent 打补丁在 Qt 下恒为 0）"
    )
    w.close()


def test_filter_does_not_swallow_events(qapp, monkeypatch):
    """过滤器必须返回 False：吞掉 Paint 事件会让控件永久不刷新。"""
    monkeypatch.setenv(paint_profiler._ENV_KEY, "1")
    assert paint_profiler.start(window_s=5) is True

    painted = {"n": 0}

    class _Visible(QWidget):
        def paintEvent(self, e):  # noqa: N802 - Qt 命名
            painted["n"] += 1
            p = QPainter(self)
            p.fillRect(self.rect(), QColor("#234"))
            p.end()

    w = _Visible()
    w.resize(100, 80)
    w.show()

    from PySide6.QtCore import QEventLoop

    loop = QEventLoop()
    QTimer.singleShot(0, w.update)
    QTimer.singleShot(600, loop.quit)
    loop.exec()

    assert painted["n"] >= 1, "控件没被重绘 —— 过滤器吞掉了 Paint 事件"
    w.close()


def test_stop_is_idempotent(qapp, monkeypatch):
    monkeypatch.setenv(paint_profiler._ENV_KEY, "1")
    assert paint_profiler.start(window_s=5) is True
    paint_profiler.stop()
    assert paint_profiler._started is False
    paint_profiler.stop()  # 二次调用不得抛
    assert paint_profiler._started is False


def test_report_does_not_crash_with_no_data(qapp):
    """无数据时打印报告不得抛（窗口到期必然走这条路径）。"""
    paint_profiler.reset()
    paint_profiler.report()


def test_reset_keeps_filter_writing(qapp, monkeypatch):
    """周期复位后统计仍能累积（锁住 clear() vs 重新绑定的坑）。

    reset() 若把 `_stats` 重新绑定成新 dict，而过滤器闭包持有旧引用，
    第二轮采样起会恒为空 —— 表现为「采样器装了但永远报 0 次」。
    """
    monkeypatch.setenv(paint_profiler._ENV_KEY, "1")
    assert paint_profiler.start(window_s=60) is True

    class _Cycle(QWidget):
        def paintEvent(self, e):  # noqa: N802 - Qt 命名
            QPainter(self).fillRect(self.rect(), QColor("#567"))

    w = _Cycle()
    w.resize(90, 70)
    w.show()

    from PySide6.QtCore import QEventLoop

    loop = QEventLoop()
    QTimer.singleShot(0, w.update)
    QTimer.singleShot(300, loop.quit)
    loop.exec()
    first = paint_profiler._stats.get("_Cycle", 0)
    assert first >= 1, "首轮未统计到"

    # 模拟一轮采样结束
    paint_profiler.reset()
    assert paint_profiler._stats.get("_Cycle", 0) == 0, "reset 未清空"

    loop2 = QEventLoop()
    QTimer.singleShot(0, w.update)
    QTimer.singleShot(300, loop2.quit)
    loop2.exec()
    second = paint_profiler._stats.get("_Cycle", 0)
    assert second >= 1, (
        "复位后统计恒为 0 —— reset() 重新绑定了 _stats，过滤器闭包写进了废弃字典"
    )
    w.close()
