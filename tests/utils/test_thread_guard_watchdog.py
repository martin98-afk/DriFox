# -*- coding: utf-8 -*-
"""thread_guard 看门狗骨架测试（#2.8 清单 3）"""
from app.utils.thread_guard import (
    _STUCK_TIMEOUT_S,
    _WATCHDOG_INTERVAL_S,
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