# -*- coding: utf-8 -*-
"""MainThreadWatchdog — 主线程阻塞看门狗。

问题背景：启动期主线程被无打点的同步任务占用时（实测一次 3.1s），现有日志
只能看到「让帧后等待 N ms」的结果（_load_ui_plugins_step 的 yield_at 机制），
指名不了占用者。心跳 QTimer 在阻塞期间不触发、阻塞结束后栈已消失，主线程
自证不可能；必须由后台线程在阻塞持续期间抓主线程栈。

机制：
- 主线程心跳：QTimer 每 200ms 更新一次 monotonic 时间戳（事件循环活着就跳）。
- 后台监控线程：50ms 粒度检查时间戳；间隙 ≥ 阈值且未报告 → sys._current_frames()
  抓主线程当前栈写 WARNING（同一次阻塞只报一次，心跳恢复后复位）。
- 首次心跳到来前不检测：main() 的 exec_() 前是同步初始化段，没有事件循环，
  心跳本来就不跳，不计入（那段已有各自日志）。

纯观测不改行为；daemon 线程随进程退出，无需显式停。
"""

import sys
import threading
import time
import traceback

from loguru import logger
from PyQt5.QtCore import QTimer

_HEARTBEAT_INTERVAL_MS = 200  # 心跳间隔：事件循环正常调度抖动远小于此
_CHECK_INTERVAL_S = 0.05  # 监控线程检查粒度
_MAX_STACK_FRAMES = 30  # 单次报告栈帧上限（防日志爆炸）


class MainThreadWatchdog:
    """主线程阻塞观测器：QTimer 心跳 + 后台线程抓栈。进程级一份。"""

    def __init__(self, threshold_ms: int = 500, parent=None):
        self._threshold_s = threshold_ms / 1000.0
        self._last_tick = time.monotonic()
        self._heartbeating = False  # 首次心跳前不检测（exec_ 前无事件循环）
        self._reported = False  # 同一次阻塞只报一次
        self._main_ident = threading.main_thread().ident

        self._timer = QTimer(parent)
        self._timer.setInterval(_HEARTBEAT_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

        self._monitor = threading.Thread(
            target=self._monitor_loop, daemon=True, name="MainThreadWatchdog"
        )

    def start(self) -> None:
        """启动心跳与监控线程（主线程调用）。"""
        self._monitor.start()
        self._timer.start()

    def _tick(self) -> None:
        self._last_tick = time.monotonic()
        self._heartbeating = True
        self._reported = False

    def _monitor_loop(self) -> None:
        while True:
            time.sleep(_CHECK_INTERVAL_S)
            if not self._heartbeating or self._reported:
                continue
            gap_s = time.monotonic() - self._last_tick
            if gap_s < self._threshold_s:
                continue
            self._reported = True
            self._report(gap_s)

    def _report(self, gap_s: float) -> None:
        stack_lines = []
        try:
            frame = sys._current_frames().get(self._main_ident)
            if frame is not None:
                stack_lines = traceback.format_stack(frame, limit=_MAX_STACK_FRAMES)
        except Exception:  # noqa: BLE001 — 抓栈失败不掩盖阻塞事实本身
            pass
        body = "".join(stack_lines).rstrip() or "<无法抓取主线程栈>"
        logger.warning(
            f"[MainThreadWatchdog] 主线程阻塞 ≥{self._threshold_s * 1000:.0f}ms"
            f"（当前间隙 {gap_s * 1000:.0f}ms），阻塞期调用栈：\n{body}"
        )


_watchdog = None


def install_watchdog(threshold_ms: int = 500) -> None:
    """主线程调用：启动阻塞观测（QApplication 创建后、事件循环启动前均可）。"""
    global _watchdog
    if _watchdog is not None:
        return
    from PyQt5.QtWidgets import QApplication

    _watchdog = MainThreadWatchdog(threshold_ms=threshold_ms, parent=QApplication.instance())
    _watchdog.start()
