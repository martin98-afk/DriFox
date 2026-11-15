# -*- coding: utf-8 -*-
"""主程序启动期的埋点挂载（自 main.py 收拢，函数体逐字迁移、语义零变化）。

挂载约定：调用方用 try/except 全包，启动期挂载失败静默跳过（诊断设施
绝不能阻断启动）；每个入口自带 env 门控，关闭时返回 None（零行为）。
"""

from __future__ import annotations

import sys
import time

__all__ = ["start_gc_tracer", "start_thread_dumper"]


def start_thread_dumper(interval_s: float = 60.0):
    """DRIFOX_CPU_TRACE=1 时启动全线程周期 dump 线程；未开时返回 None（零行为）。

    T12/G7：主线程阻塞若为原生/GIL 等待，主线程栈自证不出来，需同时抓其它
    线程状态。每 interval_s 落一行 thread_dump（线程名→栈深）到
    ``.perf_out/trace_events.jsonl``；任何异常吐在采集侧，不得反噬主功能。
    """
    from app.core.infra import perf_jsonl as _perf_jsonl

    if not _perf_jsonl.env_flag("DRIFOX_CPU_TRACE"):
        return None
    import threading as _threading
    import traceback as _traceback

    def _loop():
        while True:
            time.sleep(interval_s)
            try:
                frames = sys._current_frames()
                names = {t.ident: t.name for t in _threading.enumerate()}
                _perf_jsonl.append_event(
                    "thread_dump", len(frames),
                    {"threads": {names.get(tid, str(tid)): len(_traceback.extract_stack(f))
                                 for tid, f in frames.items()}},
                )
            except Exception:  # noqa: BLE001 - 观测不得反噬主功能
                pass

    thread = _threading.Thread(target=_loop, name="thread-dumper", daemon=True)
    thread.start()
    return thread


def start_gc_tracer():
    """DRIFOX_GC_TRACE=1 时注册 gc.callbacks，记录 gen2 回收耗时与回收量。

    gen0/1 只更新时间戳（μs 级，不落盘）；gen2 每次 stop 落一行 gc_gen2
    （value = 耗时 ms，tags 带 collected/uncollectable）。
    回调内异常必须全吞：gc 回调外泄会中断回收链。未开时返回 None。
    """
    from app.core.infra import perf_jsonl as _perf_jsonl

    if not _perf_jsonl.env_flag("DRIFOX_GC_TRACE"):
        return None
    import gc

    def _on_gc(phase, info):
        try:
            if phase == "start":
                _on_gc.t0 = time.perf_counter()
                return
            # gc.callbacks 只有 start/stop 两个阶段（无 "exec"）；collect 结束在 stop
            if phase != "stop":
                return
            if int(info.get("generation", 0) or 0) < 2:
                return
            t0 = getattr(_on_gc, "t0", None)
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2) if t0 else 0.0
            _perf_jsonl.append_event(
                "gc_gen2", elapsed_ms,
                {
                    "collected": int(info.get("collected", 0) or 0),
                    "uncollectable": int(info.get("uncollectable", 0) or 0),
                },
            )
        except Exception:  # noqa: BLE001 - gc 回调异常会中断回收链
            pass

    gc.callbacks.append(_on_gc)
    return _on_gc
