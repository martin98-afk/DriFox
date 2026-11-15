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
from collections import deque

from loguru import logger
from PyQt5.QtCore import QTimer

from app.core.infra.perf_jsonl import append_event, env_flag

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

# [O2a] 阻塞事件 JSONL 落盘门控（默认关）：loguru WARNING 路径逐字不变，
# JSONL 只是增量旁路，供基线归档/自动化统计消费。
_WATCHDOG_JSONL_ENABLED = env_flag("DRIFOX_WATCHDOG_JSONL")
# [O2b] 事件循环延迟指标门控（默认关）。bare_ratio 与延迟分位数同源、同一次
# 落盘消费，故合并到这一个 env：分设 DRIFOX_BARE_RATIO 只会让同一行数据要配
# 两个开关，且缺了它 el_latency 就少了「阻塞性质」维度。
_EL_METRICS_ENABLED = env_flag("DRIFOX_EL_METRICS")
_EL_SAMPLE_MAXLEN = 1500  # 200ms × 1500 = 5min 滑窗
_EL_REPORT_INTERVAL_S = 300.0  # 5min 落一行 el_latency


def _percentile(samples: list, q: float) -> float:
    """线性插值分位数（samples 非空）。"""
    s = sorted(samples)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


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

        # [O2b] 事件循环延迟样本（gap_ms - 心跳本底）与裸栈率计数
        self._el_samples: deque = deque(maxlen=_EL_SAMPLE_MAXLEN)
        self._el_last_report = time.monotonic()
        self._bare_count = 0
        self._total_reports = 0

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
        now = time.monotonic()
        if _EL_METRICS_ENABLED and self._heartbeating:
            # 减心跳本底：静置时只剩调度抖动，否则 p95 恒 ≈200ms 全是噪音。
            # 首次心跳不采样（构造到首跳的间隔含启动期同步段，不是 EL 延迟）。
            self._el_samples.append(round((now - self._last_tick) * 1000.0 - _HEARTBEAT_INTERVAL_MS, 1))
        self._last_tick = now
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
            # [O2b] 指标落盘放在监控线程：写盘 I/O 不占主线程（5min 一次）。
            if _EL_METRICS_ENABLED:
                now = time.monotonic()
                if now - self._el_last_report >= _EL_REPORT_INTERVAL_S:
                    self._el_last_report = now
                    self._emit_metrics()
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
        # [O2a] 结构化旁路：上面的 WARNING 逐字保持，JSONL 只做增量（默认关）。
        self._total_reports += 1
        if is_bare:
            self._bare_count += 1
        if _WATCHDOG_JSONL_ENABLED:
            try:
                append_event(
                    "watchdog_block",
                    round(gap_s * 1000, 1),
                    {
                        "threshold_ms": int(self._threshold_s * 1000),
                        "bare": is_bare,
                        "other_threads": len(all_frames) - 1,
                    },
                )
            except Exception:  # noqa: BLE001 — 观测不得反噬看门狗主路径
                pass

    def _emit_metrics(self) -> None:
        """落一行 el_latency（5min 周期，监控线程调用；门控在调用侧）。"""
        try:
            samples = list(self._el_samples)
            if not samples:
                return
            p95 = _percentile(samples, 0.95)
            bare_ratio = round(self._bare_count / self._total_reports, 4) if self._total_reports else 0.0
            append_event(
                "el_latency",
                round(p95, 1),
                {
                    "p50": round(_percentile(samples, 0.50), 1),
                    "p95": round(p95, 1),
                    "p99": round(_percentile(samples, 0.99), 1),
                    "max": round(max(samples), 1),
                    "samples": len(samples),
                    "bare_ratio": bare_ratio,
                },
            )
        except Exception:  # noqa: BLE001 — 观测不得反噬看门狗主路径
            pass

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
    from PyQt5.QtWidgets import QApplication

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
