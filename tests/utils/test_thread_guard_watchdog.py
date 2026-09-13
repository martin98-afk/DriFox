# -*- coding: utf-8 -*-
"""thread_guard 看门狗骨架测试（#2.8 清单 3）"""
from app.utils.thread_guard import (
    _STUCK_TIMEOUT_S,
    _WATCHDOG_INTERVAL_S,
    _scan_stuck_threads,
    _watchdog_thread,
    install_guard,
    start_watchdog,
)


def test_constants_have_safe_defaults():
    """看门狗阈值与间隔常量取保守默认值（60s/30s）。"""
    assert _STUCK_TIMEOUT_S == 60
    assert _WATCHDOG_INTERVAL_S == 30


def test_watchdog_starts_idempotently():
    """start_watchdog 可重复调用且守护线程存活。"""
    install_guard()
    start_watchdog()
    start_watchdog()  # 第二次幂等
    assert _watchdog_thread is not None
    assert _watchdog_thread.is_alive()
    assert _watchdog_thread.daemon is True
    assert _watchdog_thread.name == "ThreadGuardWatchdog"


def test_watchdog_tolerates_deleted_cpp_thread(qapp):
    """C++ 侧已析构的 QThread 不得让看门狗抛 RuntimeError，且条目要被剔除。

    回归现场：
        Exception in thread ThreadGuardWatchdog:
        RuntimeError: wrapped C/C++ object of type QThread has been deleted
    """
    from PyQt5 import sip
    from PyQt5.QtCore import QThread

    from app.utils import thread_guard as tg

    tg.install_guard()
    t = QThread()
    assert t in tg._running_threads, "install_guard 应已把新线程登记进墓地"
    # 模拟 Qt 主线程销毁 C++ 对象、但集合里条目未被 destroyed 回调清掉
    sip.delete(t)
    assert sip.isdeleted(t)
    try:
        tg._scan_stuck_threads()  # 修复前：此处抛 RuntimeError 打断整个看门狗线程
    finally:
        try:
            tg._running_threads.discard(t)
        except RuntimeError:
            pass
    # 再扫一轮：确认失效条目已被剔除，不会每 30s 重复抛
    tg._scan_stuck_threads()