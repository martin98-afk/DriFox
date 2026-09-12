# -*- coding: utf-8 -*-
"""批4 回归门：DeferredTaskQueue 五断言（保序/once/屏障/stop 丢弃/异常不阻塞）。

队列契约（总纲 v2）：
- register(name, fn, *, priority="critical"|"idle", delay_ms=0, once=True)
- add_order_constraint(before, after)：after 执行前 before 必须已完成（违序注册仍保序）
- set_barrier(name)：屏障任务等 critical 全完成 + idle 尝试一轮后才执行
- start() / cancel(name) / stop()
- stop 后未执行的 critical 丢弃并 warning；任务异常不阻塞后续任务
"""

import time

import pytest

from PyQt5.QtCore import QEventLoop, QTimer

pytest.importorskip("PyQt5.QtWidgets")

from app.core.deferred_task_queue import DeferredTaskQueue


def _drain(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


def test_order_constraint_with_out_of_order_registration(qapp):
    """违序注册仍按约束执行：after 先注册、before 后注册，执行序必须 before→after"""
    q = DeferredTaskQueue()
    order = []
    # 故意先注册约束的 after（约束此刻引用未注册的 before）
    q.register("after", lambda: order.append("after"), priority="idle", delay_ms=0)
    q.add_order_constraint("before", "after")
    q.register("before", lambda: order.append("before"), priority="idle", delay_ms=0)
    q.start()
    _drain(400)
    assert order == ["before", "after"], f"保序失败：执行序 {order}"


def test_once_registration_idempotent(qapp):
    """同名重复 register：once 任务只执行一次"""
    q = DeferredTaskQueue()
    n = {"c": 0}
    q.register("dup", lambda: n.__setitem__("c", n["c"] + 1), priority="critical", delay_ms=0)
    q.register("dup", lambda: n.__setitem__("c", n["c"] + 1), priority="critical", delay_ms=0)
    q.start()
    _drain(300)
    assert n["c"] == 1, f"once 幂等失败：执行 {n['c']} 次"


def test_barrier_waits_critical(qapp):
    """屏障任务等 critical 全完成：barrier 执行时刻 critical 必须已完成"""
    q = DeferredTaskQueue()
    done_at = {}

    def critical_fn():
        time.sleep(0.02)
        done_at["critical"] = time.perf_counter()

    def barrier_fn():
        done_at["barrier"] = time.perf_counter()

    q.register("crit", critical_fn, priority="critical", delay_ms=0)
    q.register("bar", barrier_fn, priority="idle", delay_ms=0)
    q.set_barrier("bar")
    q.start()
    _drain(600)
    assert "critical" in done_at and "barrier" in done_at, f"任务未全执行：{done_at.keys()}"
    assert done_at["barrier"] >= done_at["critical"], "屏障任务先于 critical 执行（屏障失效）"


def test_stop_discards_pending_critical(qapp):
    """stop 后未执行任务全部丢弃（critical 不再执行）"""
    q = DeferredTaskQueue()
    n = {"c": 0}
    q.register("late", lambda: n.__setitem__("c", n["c"] + 1), priority="critical", delay_ms=10_000)
    q.start()
    q.stop()
    _drain(300)
    assert n["c"] == 0, "stop 后任务仍被执行"


def test_task_exception_does_not_block(qapp):
    """任务抛异常：记录后继续，后续任务照常执行"""
    q = DeferredTaskQueue()

    def boom():
        raise RuntimeError("boom")

    ran = {"n": False}
    q.register("bad", boom, priority="critical", delay_ms=0)
    q.register("next", lambda: ran.__setitem__("n", True), priority="critical", delay_ms=0)
    q.start()
    _drain(400)
    assert ran["n"] is True, "异常任务阻塞了后续任务"
