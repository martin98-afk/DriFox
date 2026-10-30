# -*- coding: utf-8 -*-
"""批4 回归门：DeferredTaskQueue 五断言（保序/once/屏障/stop 丢弃/异常不阻塞）。

队列契约（总纲 v2）：
- register(name, fn, *, priority="critical"|"idle", delay_ms=0, once=True)
- add_order_constraint(before, after)：after 执行前 before 必须已完成（违序注册仍保序）
- set_barrier(name)：屏障任务等 critical 全完成 + idle 尝试一轮后才执行
- start() / cancel(name) / stop()
- stop 后未执行的 critical 丢弃并 warning；任务异常不阻塞后续任务
- register_async(name, fn, *, delay_ms)：fn 在后台线程执行，完成后回主线程
  标记 ran；保序约束与屏障语义对异步任务同样生效（等待期间主线程不被阻塞）
"""

import threading
import time

import pytest

from PyQt5.QtCore import QEventLoop, QTimer

pytest.importorskip("PyQt5.QtWidgets")

from app.core.infra.deferred_task_queue import DeferredTaskQueue


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


def test_critical_priority_barrier_does_not_self_lock(qapp):
    """屏障任务注册为 critical 时不得自锁（main_widget 的 initialization_complete 用法）。

    回归背景（2026-09-15）：main_widget.showEvent 以
    ``register("initialization_complete", priority="critical")`` +
    ``set_barrier("initialization_complete")`` 注册，旧实现 ``_ready`` 的屏障
    判定把「自己这个未完成的 critical」也算作阻塞源 → 恒不可执行 →
    ``_on_initialization_complete`` 永不触发，泵永不收口（每 5ms 空转）。
    日志证据：每次会话退出都出现
    「stop(...)：未执行的 critical 任务丢弃 ['initialization_complete']」。
    """
    q = DeferredTaskQueue()
    ran = []
    q.register("crit", lambda: ran.append("crit"), priority="critical", delay_ms=0)
    q.register("init_done", lambda: ran.append("init_done"), priority="critical", delay_ms=0)
    q.set_barrier("init_done")
    q.start()
    _drain(500)
    assert "init_done" in ran, f"critical 屏障任务自锁，未被执行：{ran}"
    assert ran.index("init_done") > ran.index("crit"), f"屏障先于 critical 执行（屏障失效）：{ran}"


def test_barrier_task_excluded_from_critical_gate(qapp):
    """屏障任务不应被计入 _all_critical_done（否则 idle 屏障永不放行）。"""
    q = DeferredTaskQueue()
    ran = []
    q.register("crit", lambda: ran.append("crit"), priority="critical", delay_ms=0)
    q.register("late_barrier", lambda: ran.append("late_barrier"), priority="critical", delay_ms=0)
    q.register("idle_barrier", lambda: ran.append("idle_barrier"), priority="idle", delay_ms=0)
    q.set_barrier("late_barrier")
    q.set_barrier("idle_barrier")
    q.start()
    _drain(500)
    assert q._all_critical_done() is True, "屏障任务被误计入 critical 完成度"
    assert set(ran) == {"crit", "late_barrier", "idle_barrier"}, f"任务未全执行：{ran}"


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


def test_async_task_runs_off_main_thread(qapp):
    """register_async：任务体在非主线程执行，完成后回主线程标记 ran。

    背景：运行时组件 warmup 同步执行 7.4s 无 yield，主线程硬冻结；改后台线程后
    主线程只投递不等待，任务体必须真的离开主线程，否则优化无效。
    """
    q = DeferredTaskQueue()
    main_ident = threading.get_ident()
    seen = {}

    def bg():
        seen["ident"] = threading.get_ident()
        time.sleep(0.02)

    q.register_async("bg", bg, delay_ms=0)
    q.start()
    _drain(500)
    assert seen.get("ident") not in (None, main_ident), f"任务体仍在主线程执行: {seen}"
    assert q._tasks["bg"].ran is True, "后台任务完成后未回填 ran"


def test_async_task_does_not_block_main_thread(qapp):
    """异步任务执行期间主线程定时器照常触发（事件循环不被冻结）。

    这条锁的是本次优化的核心契约：改动前 runtime_components 同步跑 7.4s，
    期间主线程零响应（拖动/输入全卡）；异步化后同样的任务体只占后台线程。
    """
    q = DeferredTaskQueue()
    ticks = {"n": 0}
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.__setitem__("n", ticks["n"] + 1))
    timer.start()
    try:
        q.register_async("slow", lambda: time.sleep(0.3), delay_ms=0)
        q.start()
        _drain(400)
    finally:
        timer.stop()
    assert ticks["n"] >= 15, f"主线程在异步任务执行期间被阻塞：10ms 定时器只触发 {ticks['n']} 次"


def test_async_task_blocks_constraint_until_done(qapp):
    """保序：before 是异步任务时，after 必须等它真正完成才执行。

    背景：gateway_sync 依赖 runtime_components 注册的平台；异步化后若按「已投递」
    就放行，gateway_sync 会看到空平台表漏启连接。
    """
    q = DeferredTaskQueue()
    done_at = {}

    def bg():
        time.sleep(0.05)
        done_at["bg"] = time.perf_counter()

    q.register_async("bg", bg, delay_ms=0)
    q.register("after", lambda: done_at.__setitem__("after", time.perf_counter()), priority="idle", delay_ms=0)
    q.add_order_constraint("bg", "after")
    q.start()
    _drain(600)
    assert "bg" in done_at and "after" in done_at, f"任务未全执行：{done_at.keys()}"
    assert done_at["after"] >= done_at["bg"], "after 先于异步 before 完成（保序失效）"


def test_async_task_exception_does_not_block(qapp):
    """异步任务抛异常：回填 ran 并继续后续任务，不把泵卡死"""
    q = DeferredTaskQueue()

    def boom():
        raise RuntimeError("boom")

    ran = {"n": False}
    q.register_async("bad_async", boom, delay_ms=0)
    q.register("after", lambda: ran.__setitem__("n", True), priority="idle", delay_ms=0)
    q.add_order_constraint("bad_async", "after")
    q.start()
    _drain(500)
    assert q._tasks["bad_async"].ran is True, "异常异步任务未回填 ran（泵会永久等待）"
    assert ran["n"] is True, "异常异步任务阻塞了后续任务"
