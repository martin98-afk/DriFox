# -*- coding: utf-8 -*-
"""
全局 QThread 安全守卫 — 系统级单点防护

安装方式：在 app/__init__.py 中导入一次：
    from app.utils.thread_guard import install_guard
    install_guard()

原理：
    Monkey-patch QThread.__init__，对所有 QThread 实例自动执行：
    1. 重设 parent 为全局隐藏 QObject — 即使卡片创建了 QThread(self)，
       widget 销毁时 QThread 也不会被 Qt 父链级联销毁。
    2. 全局强引用跟踪 — Python GC 无法回收仍在运行中的 QThread，
       即使卡片代码执行了 self._worker_thread = None。

适用场景：
    - 热重载卸载卡片
    - 关闭聊天窗口
    - QThread 引用丢失 + Python GC
    - 任何 QThread 先于底层 OS 线程被销毁的路径
"""

import logging as _logging
import threading as _threading
import time as _time
from typing import Set

from PySide6.QtCore import QObject, QThread

# ── 全局隐藏 QObject ──────────────────────────────────
# 所有 QThread 的 parent 被重定向到此对象，生命周期 = 应用进程。
# 任何 widget 销毁链都无法波及此对象下的 QThread。
_thread_anchor: QObject = QObject()

# ── 全局强引用集合 ────────────────────────────────────
# 保持对 ALL 运行中 QThread 的强引用，防止 Python GC 提前回收。
_running_threads: Set[QThread] = set()


def _on_thread_finished(thread: QThread) -> None:
    """线程正常结束后从墓地移除"""
    # 防御：防止 QThread 子类覆写 finished 信号导致传入非 QThread 对象
    if isinstance(thread, QThread):
        _running_threads.discard(thread)


def _on_thread_destroyed(thread: QThread) -> None:
    """线程被销毁后从墓地移除"""
    # 防御：防止 QThread 子类覆写 destroyed 信号导致传入非 QThread 对象
    if isinstance(thread, QThread):
        _running_threads.discard(thread)


def install_guard() -> None:
    """安装 QThread 安全守卫（monkey-patch QThread.__init__）

    此函数可多次调用，幂等。
    """
    # 检查是否已安装
    if getattr(QThread, "__init__", None) is getattr(
        install_guard, "_patched", None
    ):
        return

    original_init = QThread.__init__

    def _safe_init(self, parent=None):
        # ★ 看门狗起点：记录创建时间戳，用于卡死检测
        self._guard_start_ts = _time.monotonic()
        # ── 1. 重设 parent → 全局隐藏锚点 ──
        # 不管调用方传了什么 parent（哪怕是 widget），
        # 都改为 _thread_anchor，防止 widget 销毁时连带销毁运行中的 QThread
        original_init(self, _thread_anchor)

        # ── 2. 全局强引用跟踪 ──
        # 即使卡片代码执行 self._worker_thread = None，
        # Python GC 也无法回收此 QThread，因为 _running_threads 持有引用
        _running_threads.add(self)
        self.finished.connect(lambda t=self: _on_thread_finished(t))
        self.destroyed.connect(lambda t=self: _on_thread_destroyed(t))

    QThread.__init__ = _safe_init
    # 标记已安装
    install_guard._patched = _safe_init  # type: ignore[attr-defined]


# ── 看门狗：定期扫描 _running_threads，检测卡死线程 ──
_STUCK_TIMEOUT_S = 60        # 卡死阈值（秒）：线程创建后超过此时间未结束视为可疑
_WATCHDOG_INTERVAL_S = 30    # 扫描间隔（秒）
_watchdog_lock = _threading.Lock()
_watchdog_thread = None
_watchdog_logger = _logging.getLogger("thread_guard.watchdog")


def _watchdog_loop() -> None:
    """看门狗循环：每 _WATCHDOG_INTERVAL_S 秒扫描一次 _running_threads。

    对每个仍在运行的 QThread，若自创建起超过 _STUCK_TIMEOUT_S 秒仍未结束，
    输出 WARNING 日志（仅记录，不自动终止——避免误杀长任务）。
    """
    while True:
        _time.sleep(_WATCHDOG_INTERVAL_S)
        now = _time.monotonic()
        with _watchdog_lock:
            snapshot = list(_running_threads)
        for thread in snapshot:
            if not isinstance(thread, QThread):
                continue
            if not thread.isRunning():
                continue
            start_ts = getattr(thread, "_guard_start_ts", None)
            if start_ts is None:
                continue
            elapsed = now - start_ts
            if elapsed > _STUCK_TIMEOUT_S:
                _watchdog_logger.warning(
                    "[ThreadGuard] 检测到疑似卡死线程: %s (已运行 %.0fs, 阈值 %ds)",
                    type(thread).__name__,
                    elapsed,
                    _STUCK_TIMEOUT_S,
                )


def start_watchdog() -> None:
    """启动看门狗后台线程（幂等）。

    守护线程，进程退出时自动结束。需在 install_guard() 之后调用。
    """
    global _watchdog_thread
    with _watchdog_lock:
        if _watchdog_thread is not None and _watchdog_thread.is_alive():
            return
        _watchdog_thread = _threading.Thread(
            target=_watchdog_loop,
            daemon=True,
            name="ThreadGuardWatchdog",
        )
        _watchdog_thread.start()
