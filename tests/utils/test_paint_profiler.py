# -*- coding: utf-8 -*-
"""paint_profiler 行为测试。

覆盖三条关键约束（都是实现时踩过或容易踩的坑）：
1. 默认关闭：没有环境变量时 ``start()`` 直接返回 False，不装任何过滤器
   （诊断设施不能给正常启动增加成本）。
2. 过滤器能按类统计 Paint 事件：Qt 的 Paint 派发必经
   ``QApplication`` 事件过滤器，这是唯一有效的统计点；给
   ``QWidget.paintEvent`` 打 Python 补丁是无效的（C++ 虚函数调度，
   计数恒为 0），本测试用真实绘制路径锁住正确实现。
3. 过滤器绝不吞事件：``eventFilter`` 必须返回 False，否则控件不再重绘。
"""

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt, QTimer  # noqa: E402
from PyQt5.QtGui import QColor, QPainter  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

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


def test_disabled_by_default(qapp):
    """没有环境变量时不得启用，也不得安装过滤器。"""
    assert paint_profiler.enabled() is False
    assert paint_profiler.start() is False
    assert paint_profiler._started is False


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_disabled_values(qapp, monkeypatch, value):
    monkeypatch.setenv(paint_profiler._ENV_KEY, value)
    assert paint_profiler.enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_enabled_values(monkeypatch, value):
    monkeypatch.setenv(paint_profiler._ENV_KEY, value)
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

    from PyQt5.QtCore import QEventLoop

    loop = QEventLoop()
    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(15)
    QTimer.singleShot(8000, loop.quit)
    loop.exec_()

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

    from PyQt5.QtCore import QEventLoop

    loop = QEventLoop()
    QTimer.singleShot(0, w.update)
    QTimer.singleShot(600, loop.quit)
    loop.exec_()

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
