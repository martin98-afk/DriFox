# -*- coding: utf-8 -*-
"""Profiling 辅助：复现「慢 hook 阻塞 backend_create」机制的占位函数。仅供 harness 调用。"""
import os
import sys
import time

# 模块全局睡眠时长（harness 在 trigger 前设置，避开 os.environ 在 worker 线程的传递坑）
_SEC: float = 8.7


def set_sec(secs: float) -> None:
    global _SEC
    _SEC = float(secs)


def blocking_sleep(event, context) -> str:
    """python 注入型(add_output=True) 慢 hook：worker 线程 + UI QEventLoop 等待 → 计入 backend_create。"""
    print(f"[slow_hooks] blocking_sleep ENTER sec={_SEC} pid={os.getpid()}", file=sys.stderr, flush=True)
    t0 = time.perf_counter()
    time.sleep(_SEC)
    dt = time.perf_counter() - t0
    print(f"[slow_hooks] blocking_sleep DONE dt={dt:.3f}s", file=sys.stderr, flush=True)
    return f"blocking-sleep-done-{_SEC}s"


def async_sleep(event, context) -> str:
    """python 非注入型(add_output=False) 慢 hook：后台异步不阻塞。"""
    return blocking_sleep(event, context)