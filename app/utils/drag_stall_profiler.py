"""拖拽卡顿采样器 — py-spy 式主线程阻塞现场抓取

用途：窗口拖拽期间偶发卡顿难以静态定位时，启用本采样器抓取卡顿瞬间
主线程正在执行的 Python 调用栈，直接定位元凶，替代反复猜测。

原理：
1. 主线程上跑一个 10ms 心跳 QTimer，不断刷新 `_heartbeat` 时间戳；
2. 后台守护线程每 5ms 检查心跳距今是否超过阈值（默认 35ms）；
3. 超阈值 = 主线程被某个回调阻塞 → 用 `sys._current_frames()` 抓取
   主线程当前调用栈写入日志（tag: [DRAG-PROF]）；
4. 拖拽结束后输出聚合报告：卡顿次数、最长阻塞、Top 热点栈。

开销：心跳定时器每次回调仅一次 perf_counter 赋值；采样线程仅在检测到
阻塞时才做栈提取。对拖拽流畅度本身影响可忽略。

使用（已接入 TabManagerWindow）：
    from app.utils.drag_stall_profiler import drag_profiler
    drag_profiler.start()          # 拖拽开始（WM_ENTERSIZEMOVE）
    drag_profiler.stop_deferred()  # 拖拽结束（延迟 1.5s 停止，覆盖松手卡顿）

日志关键字：`[DRAG-PROF]`，与主日志同文件（logs/llm_chatter.log）。
定位完成后可将 `DRAG_PROFILER_ENABLED` 置 False 一键关闭。
"""

import sys
import threading
import time
import traceback

from loguru import logger
from PyQt5.QtCore import Qt, QTimer

# 一键开关：定位完成后置 False，start()/stop() 变为空操作
DRAG_PROFILER_ENABLED = False

# 主线程阻塞判定阈值（秒）。35ms ≈ 掉 2 帧（60fps），肉眼可感知
_STALL_THRESHOLD = 0.035
# 心跳定时器间隔（ms）
_HEARTBEAT_INTERVAL_MS = 10
# 采样线程轮询间隔（秒）
_POLL_INTERVAL = 0.005
# 单次卡顿持续期间的重复采样间隔（秒），用于抓长阻塞的栈演变
_RESAMPLE_INTERVAL = 0.02
# 栈签名去重时取栈顶帧数
_SIG_DEPTH = 6
# 裸栈（主线程栈只剩 app.exec_，阻塞在原生代码或等 GIL）时全线程 dump 的最小间隔（秒）
_FULL_DUMP_INTERVAL = 1.0
# 裸栈判定：主线程栈帧数 ≤ 该值视为"无 Python 现场"
_BARE_STACK_DEPTH = 3


class DragStallProfiler:
    """拖拽期间主线程阻塞采样器（单例使用 `drag_profiler`）"""

    def __init__(self):
        self._active = False
        self._thread: threading.Thread | None = None
        self._heartbeat = 0.0
        self._heartbeat_timer: QTimer | None = None
        self._main_tid = threading.main_thread().ident
        self._stop_pending = False
        # 统计
        self._stall_count = 0
        self._max_stall = 0.0
        self._started_at = 0.0
        self._sig_stats: dict[tuple, list] = {}  # sig -> [count, 代表性栈文本]
        self._last_full_dump = 0.0  # 上次全线程 dump 时间

    # ── 生命周期 ──────────────────────────────────────────────

    def start(self):
        """拖拽开始时调用（主线程）。重复调用安全（幂等）。"""
        if not DRAG_PROFILER_ENABLED or self._active:
            self._stop_pending = False  # 松手后 1.5s 内再次拖拽：取消延迟停止
            return
        self._active = True
        self._stop_pending = False
        self._stall_count = 0
        self._max_stall = 0.0
        self._sig_stats.clear()
        self._started_at = time.perf_counter()
        self._heartbeat = self._started_at

        if self._heartbeat_timer is None:
            self._heartbeat_timer = QTimer()
            self._heartbeat_timer.setTimerType(Qt.PreciseTimer)
            self._heartbeat_timer.setInterval(_HEARTBEAT_INTERVAL_MS)
            self._heartbeat_timer.timeout.connect(self._beat)
        self._heartbeat_timer.start()

        self._thread = threading.Thread(target=self._sampler_loop, name="drag-stall-sampler", daemon=True)
        self._thread.start()
        logger.debug("[DRAG-PROF] 采样开始（阈值 {}ms）", int(_STALL_THRESHOLD * 1000))

    def stop_deferred(self, delay_ms: int = 1500):
        """拖拽结束时调用：延迟停止，覆盖"松手瞬间卡顿"窗口期"""
        if not self._active:
            return
        self._stop_pending = True
        QTimer.singleShot(delay_ms, self._stop_if_pending)

    def _stop_if_pending(self):
        # start() 在延迟窗口内被再次调用会清掉 pending（用户又开始拖了）
        if self._stop_pending:
            self.stop()

    def stop(self):
        if not self._active:
            return
        self._active = False
        self._stop_pending = False
        if self._heartbeat_timer is not None:
            self._heartbeat_timer.stop()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
            self._thread = None
        self._report()

    # ── 内部 ──────────────────────────────────────────────────

    def _beat(self):
        self._heartbeat = time.perf_counter()

    def _sampler_loop(self):
        stall_started = None  # 当前卡顿的开始时间；None = 未在卡顿中
        last_sample = 0.0
        stall_had_python = False  # 本次卡顿是否抓到过真实 Python 调用栈
        while self._active:
            time.sleep(_POLL_INTERVAL)
            now = time.perf_counter()
            gap = now - self._heartbeat
            if gap >= _STALL_THRESHOLD:
                if stall_started is None:
                    stall_started = self._heartbeat  # 卡顿起点 ≈ 最后一次心跳
                    stall_had_python = False
                if now - last_sample >= _RESAMPLE_INTERVAL:
                    last_sample = now
                    if self._capture_stack(gap):
                        stall_had_python = True
            else:
                if stall_started is not None:
                    duration = now - stall_started
                    if stall_had_python:
                        # 真实卡顿：主线程确实在跑 Python 代码（重绘/重算/I/O）
                        self._max_stall = max(self._max_stall, duration)
                        logger.warning("[DRAG-PROF] 卡顿结束，主线程被阻塞 {:.0f}ms", duration * 1000)
                    else:
                        # 裸栈卡顿：主线程卡在原生拖拽模态循环（DefWindowProc），
                        # 是拖拽时长本身（心跳 QTimer 被 OS 模态循环饿死），
                        # 非真实卡顿，仅 debug 记录、不计入卡顿统计。
                        logger.debug(
                            "[DRAG-PROF] 拖拽模态循环/原生阻塞 {:.0f}ms（预期，非真实卡顿）",
                            duration * 1000,
                        )
                    stall_started = None

    def _capture_stack(self, gap: float) -> bool:
        """抓取主线程当前调用栈。

        返回 True 表示抓到"真实 Python 调用栈"（即主线程确实在跑 Python 代码，
        属于真实卡顿，应计入统计）；返回 False 表示裸栈（阻塞在原生代码/等 GIL，
        拖拽期间多为 OS 模态循环，属预期）。
        """
        try:
            all_frames = sys._current_frames()
            frame = all_frames.get(self._main_tid)
            if frame is None:
                return False
            stack = traceback.extract_stack(frame, limit=25)
            is_bare = len(stack) <= _BARE_STACK_DEPTH
            if is_bare:
                # 裸栈：不计入热点统计，仅限频 dump 后台线程栈到 debug 供排查
                now = time.perf_counter()
                if now - self._last_full_dump >= _FULL_DUMP_INTERVAL:
                    self._last_full_dump = now
                    dump = self._format_other_threads(all_frames)
                    if dump:
                        logger.debug(
                            "[DRAG-PROF] 裸栈全线程 dump（拖拽模态循环期间，非真实卡顿）：\n{}",
                            dump,
                        )
                return False

            # ── 真实 Python 调用栈：这是主线程在拖拽期间实际执行的活儿 ──
            # 签名：栈顶 N 帧的 (文件, 行号)，用于去重聚合
            sig = tuple((f.filename, f.lineno) for f in stack[-_SIG_DEPTH:])
            text = "".join(traceback.format_list(stack))
            entry = self._sig_stats.get(sig)
            if entry is None:
                self._sig_stats[sig] = [1, text]
                logger.warning("[DRAG-PROF] 主线程阻塞 {:.0f}ms，调用栈：\n{}", gap * 1000, text)
                self._stall_count += 1  # 仅真实 Python 栈计入卡顿次数
            else:
                entry[0] += 1
            return True
        except Exception:  # 采样绝不能影响主程序
            return False

    def _format_other_threads(self, all_frames: dict) -> str:
        """格式化除主线程与采样线程外的所有线程栈（每线程栈顶 8 帧）"""
        names = {t.ident: t.name for t in threading.enumerate()}
        my_tid = threading.get_ident()
        parts = []
        for tid, frm in all_frames.items():
            if tid in (self._main_tid, my_tid):
                continue
            try:
                stack = traceback.extract_stack(frm, limit=8)
                top = stack[-1] if stack else None
                # 过滤纯等待线程（wait/sleep/get/poll 顶帧），聚焦真正在跑代码的线程
                if top is not None and top.name in ("wait", "sleep", "get", "poll", "select", "accept", "recv"):
                    continue
                text = "".join(traceback.format_list(stack))
                parts.append(f"── 线程 [{names.get(tid, tid)}] ──\n{text}")
            except Exception:
                continue
        return "\n".join(parts)

    def _report(self):
        total = time.perf_counter() - self._started_at
        if self._stall_count == 0:
            logger.info("[DRAG-PROF] 采样结束（{:.1f}s）：未检测到 ≥{}ms 的主线程阻塞", total, int(_STALL_THRESHOLD * 1000))
            return
        logger.warning(
            "[DRAG-PROF] 采样结束（{:.1f}s）：卡顿 {} 次，最长阻塞 {:.0f}ms，热点 Top{}：",
            total,
            self._stall_count,
            self._max_stall * 1000,
            min(5, len(self._sig_stats)),
        )
        ranked = sorted(self._sig_stats.items(), key=lambda kv: kv[1][0], reverse=True)[:5]
        for i, (_sig, (count, text)) in enumerate(ranked, 1):
            # 只打热点栈的最后 6 行（app 代码通常在栈底附近已可见）
            tail = "".join(text.splitlines(keepends=True)[-12:])
            logger.warning("[DRAG-PROF] 热点 #{}（采样 {} 次）：\n{}", i, count, tail)


drag_profiler = DragStallProfiler()
