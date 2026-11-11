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
from PySide6.QtCore import QTimer

_HEARTBEAT_INTERVAL_MS = 200  # 心跳间隔：事件循环正常调度抖动远小于此
_CHECK_INTERVAL_S = 0.05  # 监控线程检查粒度
_MAX_STACK_FRAMES = 30  # 单次报告栈帧上限（防日志爆炸）
# 裸栈判定：主线程 Python 栈帧数 ≤ 该值（只剩 exec_ 前的壳）＝阻塞在
# 原生代码（Qt/Chromium）或等 GIL，Python 栈自证不了元凶
_BARE_STACK_DEPTH = 3
# 裸栈全线程 dump 最小间隔（秒）：连续阻塞时防日志爆炸
_BARE_DUMP_MIN_INTERVAL_S = 2.0
# 全线程 dump 时过滤的纯等待顶帧名（threading/queue/socket 的常规泊位）
_IDLE_TOP_FRAMES = {"wait", "sleep", "get", "poll", "select", "accept", "recv", "acquire"}
# 全线程 dump 每线程帧数上限
_BARE_DUMP_FRAMES_PER_THREAD = 8


class MainThreadWatchdog:
    """主线程阻塞观测器：QTimer 心跳 + 后台线程抓栈。进程级一份。"""

    def __init__(self, threshold_ms: int = 500, parent=None):
        self._threshold_s = threshold_ms / 1000.0
        self._last_tick = time.monotonic()
        self._heartbeating = False  # 首次心跳前不检测（exec_ 前无事件循环）
        self._reported = False  # 同一次阻塞只报一次
        self._suspended = False  # 已知合法阻塞期间（SC_MOVE 模态循环）暂停判定
        self._main_ident = threading.main_thread().ident
        self._last_bare_dump = 0.0  # 上次裸栈全线程 dump 时间（monotonic）

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

    def set_suspended(self, value: bool) -> None:
        """暂停/恢复阻塞判定（主线程或任意线程调用均可）。

        恢复时重置心跳基线：暂停期间心跳 QTimer 可能长时间未跳，
        不重置会把暂停期整段计入 gap 造成恢复后误报一次。
        """
        self._suspended = value
        if not value:
            self._last_tick = time.monotonic()
            self._reported = False

    def _monitor_loop(self) -> None:
        while True:
            time.sleep(_CHECK_INTERVAL_S)
            if self._suspended:
                continue
            if not self._heartbeating or self._reported:
                continue
            gap_s = time.monotonic() - self._last_tick
            if gap_s < self._threshold_s:
                continue
            self._reported = True
            self._report(gap_s)

    def _report(self, gap_s: float) -> None:
        stack_lines = []
        is_bare = False
        all_frames: dict = {}
        try:
            all_frames = sys._current_frames()
            frame = all_frames.get(self._main_ident)
            if frame is not None:
                stack_lines = traceback.format_stack(frame, limit=_MAX_STACK_FRAMES)
                is_bare = len(stack_lines) <= _BARE_STACK_DEPTH
        except Exception:  # noqa: BLE001 — 抓栈失败不掩盖阻塞事实本身
            pass
        body = "".join(stack_lines).rstrip() or "<无法抓取主线程栈>"
        if is_bare:
            # 裸栈：主线程卡在原生代码或等 GIL，主线程栈看不到元凶。
            # 补全线程 dump 找持 GIL 干活的后台线程（2026-10-31 长会话
            # watchdog 20 条裸栈无法归因，GIL 争抢 vs Chromium 布局存疑）。
            body += "\n" + self._dump_other_threads(all_frames)
        logger.warning(
            f"[MainThreadWatchdog] 主线程阻塞 ≥{self._threshold_s * 1000:.0f}ms"
            f"（当前间隙 {gap_s * 1000:.0f}ms），阻塞期调用栈：\n{body}"
        )

    def _dump_other_threads(self, all_frames: dict) -> str:
        """裸栈辅助现场：格式化除主线程与监控线程外的活动线程栈。

        过滤纯等待线程（wait/sleep/get 等顶帧），聚焦真正持 GIL 跑代码的
        线程；2s 限频防连续阻塞时日志爆炸。抓取失败返回占位说明。
        """
        now = time.monotonic()
        if now - self._last_bare_dump < _BARE_DUMP_MIN_INTERVAL_S:
            return "(裸栈：主线程阻塞在原生代码或等 GIL；全线程 dump 限频中)"
        self._last_bare_dump = now
        try:
            names = {t.ident: t.name for t in threading.enumerate()}
            my_ident = threading.get_ident()
            parts = []
            for tid, frm in all_frames.items():
                if tid in (self._main_ident, my_ident):
                    continue
                stack = traceback.extract_stack(frm, limit=_BARE_DUMP_FRAMES_PER_THREAD)
                top = stack[-1] if stack else None
                if top is not None and top.name in _IDLE_TOP_FRAMES:
                    continue  # 纯等待线程，非 GIL 持有者嫌疑
                text = "".join(traceback.format_list(stack))
                parts.append(f"── 线程 [{names.get(tid, tid)}] ──\n{text}")
            return (
                "(裸栈：主线程阻塞在原生代码或等 GIL；以下为仍在跑代码的线程）\n"
                + "\n".join(parts)
                if parts
                else "(裸栈：主线程阻塞在原生代码或等 GIL；无活动后台线程 → 指向原生阻塞)"
            )
        except Exception:  # noqa: BLE001 — dump 失败不掩盖阻塞事实
            return "(裸栈：主线程阻塞在原生代码或等 GIL；全线程 dump 失败)"


_watchdog = None


def install_watchdog(threshold_ms: int = 500) -> None:
    """主线程调用：启动阻塞观测（QApplication 创建后、事件循环启动前均可）。"""
    global _watchdog
    if _watchdog is not None:
        return
    from PySide6.QtWidgets import QApplication

    _watchdog = MainThreadWatchdog(threshold_ms=threshold_ms, parent=QApplication.instance())
    _watchdog.start()

def suspend_reporting() -> None:
    """暂停阻塞上报：已知合法阻塞段（如 Windows SC_MOVE 模态拖动循环）调用。

    模态循环期间 Windows 在泵消息、窗口正常移动，属「合法占用」；
    心跳 QTimer 在循环内派发延迟会让看门狗误报（2026-10-30 实证）。
    未安装看门狗时 no-op。必须与 resume_reporting 成对使用。
    """
    if _watchdog is not None:
        _watchdog.set_suspended(True)

def resume_reporting() -> None:
    """恢复阻塞上报（与 suspend_reporting 成对）。未安装看门狗时 no-op。"""
    if _watchdog is not None:
        _watchdog.set_suspended(False)
