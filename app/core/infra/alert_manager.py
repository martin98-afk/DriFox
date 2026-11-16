# -*- coding: utf-8 -*-
"""生产三级告警管理器（方案 5.3）。

三级
----
* **L1** loguru WARNING（纯记录）：RSS 斜率 / 心跳 p95 / QTimer 增量
* **L2** InfoBar（复用 memory_governor 软限触发点 + 句柄漂移）：「内存压力，已自动回收」
* **L3** perf_jsonl 埋点：governor 动作前后各一条（DRIFOX_GOVERNOR_TRACE 门控）

数据源决策（零高频主线程开销，全部复用常驻机制）
------------------------------------------------
* L1 RSS 斜率：``rss_sampler`` 后台循环快照（产品常驻、无 env 依赖）→ 默认无
  任何观测 env 时本告警仍工作
* L1 心跳 p95：仅在 ``DRIFOX_EL_METRICS`` 开启时生效（读 MainThreadWatchdog
  既有 _el_samples 滑窗，不新增采样）
* L1 QTimer / L2 句柄：本模块 tick 自带轻量低频采样（60s 一次，QTimer 计数
  每 5 tick 即 5min 一次 gc 扫描），与 O4 同模式
* 判定不新增高频 timer：单 daemon 线程 tick 60s，全部 O(快照读取)

防轰炸
------
* 同 key（进程+指标）10min ≤ 1 条
* 越限态恢复持续 5min → loguru INFO「已恢复」，此后不再重弹 InfoBar
* InfoBar 投递走模块内 QObject 信号桥（后台线程 emit → 主线程槽，Qt 自动排队）
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional, Tuple

from loguru import logger

from app.core.infra.telemetry.perf_jsonl import append_event, env_flag

_TICK_S = 60.0
_RSS_WINDOW_S = 2 * 3600.0          # L1 RSS 斜率评估窗
_RSS_SLOPE_LIMIT_MB_H = 5.0         # L1 RSS 斜率阈值
_HEARTBEAT_P95_LIMIT_MS = 100.0     # L1 心跳 p95 阈值（EL_METRICS 开启时）
_QTIMER_LIMIT_24H = 50.0            # L1 QTimer 24h 增量阈值
_GDI_DELTA_LIMIT = 50.0             # L2 句柄 10min 窗增量阈值
_L2_CONSECUTIVE = 3                 # L2 句柄连续越限周期数
_SUPPRESS_S = 10 * 60.0             # 防轰炸：同 key 10min ≤ 1 条
_RECOVER_S = 5 * 60.0               # 恢复持续 5min 记 INFO

_INFO_BAR_TEXT = "内存压力，已自动回收"


def _gdi_self() -> Optional[Tuple[int, int]]:
    """当前进程 GDI/USER 对象数（与 rss_sampler 同款加固写法）；失败 None。"""
    if os.name != "nt":
        return None
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        user32.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetGuiResources.restype = ctypes.c_uint
        hproc = kernel32.GetCurrentProcess()
        gdi = int(user32.GetGuiResources(hproc, 0))
        user = int(user32.GetGuiResources(hproc, 1))
        if gdi == 0 and user == 0 and ctypes.get_last_error() != 0:
            return None
        return gdi, user
    except Exception:
        return None


def _count_qtimers() -> int:
    """gc 扫描 QTimer 实例数（与 long_run/sampler 同口径）。"""
    try:
        import gc
    except Exception:
        return 0
    n = 0
    try:
        for obj in gc.get_objects():
            try:
                if type(obj).__name__ == "QTimer":
                    n += 1
            except Exception:
                continue
    except Exception:
        return n
    return n


def _heartbeat_p95(window_samples: int = 1500) -> Optional[float]:
    """读 MainThreadWatchdog 既有 el 样本滑窗（200ms/条，maxlen 1500=5min）的 p95。

    EL_METRICS 关 / 未安装 / 无样本时返回 None（告警自动跳过该判据）。
    """
    try:
        from app.utils.main_thread_watchdog import _EL_METRICS_ENABLED, _watchdog
    except Exception:
        return None
    if not _EL_METRICS_ENABLED or _watchdog is None:
        return None
    samples = [x for x in getattr(_watchdog, "_el_samples", ()) if isinstance(x, (int, float))]
    if not samples:
        return None
    tail = sorted(samples[-window_samples:])
    k = (len(tail) - 1) * 0.95
    lo = int(k)
    hi = min(lo + 1, len(tail) - 1)
    return tail[lo] + (tail[hi] - tail[lo]) * (k - lo)


class AlertManager:
    """三级告警单例。后台线程 tick 60s 评估；UI 侧通过 set_info_bar_poster 注入投递。"""

    _instance: Optional["AlertManager"] = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._tick_count = 0
        self._rss_series: Deque[Tuple[float, float]] = deque(maxlen=48)  # 48min @60s
        self._rss_breach_streak = 0
        self._qtimer_series: Deque[Tuple[float, int]] = deque(maxlen=288)  # 24h @5min
        self._gdi_series: Deque[Tuple[float, int]] = deque(maxlen=12)  # ~12min @60s
        self._gdi_breach_streak = 0
        # key -> {"last_emit": ts, "active": bool, "ok_since": ts|None}
        self._states: Dict[str, Dict[str, float]] = {}
        self._info_bar_poster: Optional[Callable[[str, str], None]] = None

    # ── UI 投递注入 ──────────────────────────────────────────
    def set_info_bar_poster(self, poster: Callable[[str, str], None]) -> None:
        """注入 InfoBar 投递回调（须在主线程注册；内部由调用方保证主线程切换）。"""
        with self._lock:
            self._info_bar_poster = poster

    def _post_info_bar(self, title: str, message: str) -> None:
        with self._lock:
            poster = self._info_bar_poster
        if poster is None:
            return
        try:
            poster(title, message)
        except Exception:
            logger.warning("[alert] InfoBar 投递失败（不影响告警记录）")

    # ── 生命周期 ─────────────────────────────────────────────
    @classmethod
    def get_instance(cls) -> "AlertManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def ensure_started(self) -> None:
        """幂等启动评估线程（任意线程可调；tick 60s，全部快照读取）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            t = threading.Thread(target=self._loop, daemon=True, name="alert-manager")
            self._thread = t
            t.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(_TICK_S):
            try:
                self.tick(time.time())
            except Exception as e:
                logger.warning(f"[alert] tick 异常（不影响后续评估）: {type(e).__name__}: {e}")

    # ── 评估 ─────────────────────────────────────────────────
    def tick(self, now: float) -> None:
        """一次评估。now 注入便于单测控制时钟。"""
        with self._lock:
            self._tick_count += 1

        # L1 RSS 斜率（常驻数据源，无 env 依赖）
        try:
            from app.core.infra.rss_sampler import rss_sampler

            mb = rss_sampler.rss_mb
            if mb > 0:
                self._rss_series.append((now, mb))
        except Exception:
            pass
        rss_series = list(self._rss_series)
        self._evaluate_rss_slope(rss_series, now)

        # L1 心跳 p95（仅 EL_METRICS 开启时有数据）
        p95 = _heartbeat_p95()
        if p95 is not None:
            self._raise_l1("heartbeat_p95", p95 > _HEARTBEAT_P95_LIMIT_MS,
                           f"心跳 p95={p95:.0f}ms（10min 窗，阈值 {100}ms）", now)
        else:
            self._note_ok("heartbeat_p95", now)

        # L1 QTimer 增量（每 5 tick 即 5min 采一次）
        if self._tick_count % 5 == 1 or self._tick_count == 1:
            qt = _count_qtimers()
            self._qtimer_series.append((now, qt))
        qt_series = list(self._qtimer_series)
        if qt_series and now - qt_series[0][0] >= 24 * 3600.0:
            delta = qt_series[-1][1] - qt_series[0][1]
            self._raise_l1("qtimer_24h", delta > _QTIMER_LIMIT_24H,
                           f"QTimer 24h 增量={delta:+.0f}（阈值 +{_QTIMER_LIMIT_24H:.0f}）", now)
        else:
            self._note_ok("qtimer_24h", now)

        # L2 句柄漂移（连续 3 tick 10min 窗增量 > 50）
        handles = _gdi_self()
        if handles is not None:
            self._gdi_series.append((now, handles[0]))
        gdi_series = list(self._gdi_series)
        # breach 只看「最新读数 − ≥10min 前最早基点」：历史条目残留不再反复计入
        # （R2 教训：遍历全表会把已恢复时段前的旧越限样本当 breach，恢复判定永假）
        breach = False
        if gdi_series:
            now_ts, now_g = gdi_series[-1]
            base = next((g for t, g in gdi_series if now_ts - t >= 10 * 60.0), None)
            if base is not None and now_g - base > _GDI_DELTA_LIMIT:
                breach = True
        if breach:
            self._gdi_breach_streak += 1
        else:
            self._gdi_breach_streak = 0
        if self._gdi_breach_streak >= _L2_CONSECUTIVE:
            snap = f"gdi={handles[0] if handles else '?'} user={handles[1] if handles else '?'}"
            self._emit_l2("handle_drift", "句柄漂移超阈（10min 窗连续 3 周期 >+50）", snap, now)
        else:
            self._note_ok("handle_drift", now)

    # ── L1 / 状态机 ──────────────────────────────────────────
    def _evaluate_rss_slope(self, series: List[Tuple[float, float]], now: float) -> None:
        """2h 窗 RSS 斜率（MB/h）> 阈值且连续 2 次评估越限 → L1（10min 限频兜底）。"""
        if len(series) < 2:
            return
        (t0, r0), (t1, r1) = series[0], series[-1]
        if t1 - t0 < _RSS_WINDOW_S:
            return
        slope = (r1 - r0) / max(t1 - t0, 1e-9) * 3600.0
        if slope > _RSS_SLOPE_LIMIT_MB_H:
            self._rss_breach_streak += 1
        else:
            self._rss_breach_streak = 0
        if self._rss_breach_streak >= 2:
            self._raise_l1("rss_slope", True, f"窗 {t1 - t0:.0f}s 斜率 {slope:+.1f}MB/h rss={r1:.0f}MB", now)

    def _raise_l1(self, key: str, breaching: bool, snapshot: str, now: float) -> None:
        self._apply_state(key, breaching, snapshot, now,
                          lambda k, s: self._emit_l1(k, s, now))

    def _emit_l1(self, key: str, snapshot: str, now: float) -> None:
        logger.warning(f"[alert][L1] {key} 越限 | {snapshot}")

    def _emit_l2(self, key: str, reason: str, snapshot: str, now: float) -> None:
        logger.warning(f"[alert][L2] {key} | {reason} | {snapshot}")
        self._apply_state(key, True, f"{reason} | {snapshot}", now,
                          lambda k, s: self._post_info_bar("内存压力", _INFO_BAR_TEXT + f"（{reason}；{s}）"))

    def _apply_state(self, key: str, breaching: bool, snapshot: str, now: float,
                     emitter: Callable[[str, str], None]) -> None:
        """防轰炸状态机：越限 10min 限频发射；恢复持续 5min 记 INFO。"""
        with self._lock:
            st = self._states.setdefault(key, {"last_emit": 0.0, "active": 0.0, "ok_since": 0.0})
        if breaching:
            st["active"] = 1.0
            st["ok_since"] = 0.0
            if now - st["last_emit"] >= _SUPPRESS_S:
                st["last_emit"] = now
                emitter(key, snapshot)
            return
        # 未越限：若此前处于活跃告警，恢复持续 5min 后记 INFO 一次
        if st["active"]:
            if not st["ok_since"]:
                st["ok_since"] = now
            elif now - st["ok_since"] >= _RECOVER_S:
                st["active"] = 0.0
                st["ok_since"] = 0.0
                st["last_emit"] = now
                logger.info(f"[alert][已恢复] {key} 持续 5min 未再越限（此前快照：{snapshot}）")

    def _note_ok(self, key: str, now: float) -> None:
        with self._lock:
            st = self._states.setdefault(key, {"last_emit": 0.0, "active": 0.0, "ok_since": 0.0})
        if not st["active"]:
            return
        if not st["ok_since"]:
            st["ok_since"] = now  # 恢复计时起点
            return
        if now - st["ok_since"] >= _RECOVER_S:
            st["active"] = 0.0
            st["ok_since"] = 0.0
            st["last_emit"] = now
            logger.info(f"[alert][已恢复] {key} 持续 5min 未再越限")

    # ── 供 memory_governor 调用 ─────────────────────────────
    def note_gate_soft(self, main_mb: float = 0.0, web_mb: float = 0.0, detail: str = "") -> None:
        """L2：闸门软限触发（复用 memory_governor 既有触发点，governor 调用）。"""
        snap = f"main={main_mb:.0f}MB web={web_mb:.0f}MB {detail}".strip()
        self._emit_l2("gate_soft", "闸门软限触发", snap, time.time())

    def note_governor_action(self, phase: str, detail: str) -> None:
        """L3：governor 动作前后埋点（DRIFOX_GOVERNOR_TRACE 门控，默认关静默跳过）。"""
        if not env_flag("DRIFOX_GOVERNOR_TRACE"):
            return
        append_event("governor_action", phase, {"detail": detail})


def get_instance() -> AlertManager:
    return AlertManager.get_instance()


def set_info_bar_poster(poster: Callable[[str, str], None]) -> None:
    get_instance().set_info_bar_poster(poster)


def ensure_started() -> None:
    get_instance().ensure_started()


def note_gate_soft(main_mb: float = 0.0, web_mb: float = 0.0, detail: str = "") -> None:
    get_instance().note_gate_soft(main_mb, web_mb, detail)


def note_governor_action(phase: str, detail: str) -> None:
    get_instance().note_governor_action(phase, detail)
