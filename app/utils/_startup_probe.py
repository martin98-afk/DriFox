# -*- coding: utf-8 -*-
"""一次性启动诊断打点（测完整体删除）。"""
import os
import time

_ENABLED = os.environ.get("DRIFOX_STARTUP_PROBE") == "1"
_T0 = time.perf_counter()
_SEG = _T0
_ROWS: list = []


def mark(label: str) -> None:
    """打一个分段点：记录上一段耗时。"""
    global _SEG
    if not _ENABLED:
        return
    now = time.perf_counter()
    _ROWS.append((label, (now - _SEG) * 1000, (now - _T0) * 1000))
    _SEG = now


def dump() -> None:
    """输出采集结果（启动末尾调用一次）。"""
    if not _ENABLED:
        return
    print("\n===== STARTUP PROBE =====")
    print(f"{'label':<40}{'seg_ms':>10}{'cum_ms':>10}")
    for label, seg, cum in _ROWS:
        print(f"{label:<40}{seg:>10.1f}{cum:>10.1f}")
    print("===== END PROBE =====\n", flush=True)
