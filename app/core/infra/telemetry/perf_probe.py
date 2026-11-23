# -*- coding: utf-8 -*-
"""轻量性能探针：计时 / 计数 / 内存采样，单一开关控制，默认完全关闭。

用途
----
给「优化前后对照」提供进程内可复现的数字。只做观测，不接管任何业务逻辑：
所有公开函数在关闭态都是空函数（``if not enabled(): return``），没有副作用，
不建目录、不读配置、不改任何产品行为。

硬约束（与交付要求逐条对应）
----------------------------
1. **显式开关**：``DRIFOX_PERF_PROBE=1`` 才启用；未设置/其它值一律关闭。
   另外支持：
   - ``DRIFOX_PERF_PROBE_OUT=<path>`` JSONL 落点（默认 ``.perf_out/perf_probe.jsonl``）
   - ``DRIFOX_PERF_PROBE_MEM=1`` 启用后台 RSS 采样线程（间隔
     ``DRIFOX_PERF_PROBE_MEM_MS``，默认 2000ms）
2. **打包版强制不可用**：``sys.frozen`` 或 ``sys._MEIPASS`` 为真时
   ``enabled()`` 恒为 False，环境变量也撬不开 —— 诊断设施不随发布版流出。
3. **不接管业务**：只有 ``time.perf_counter()`` 计时、``dict`` 计数、
   ``psutil`` 内存读取与 JSONL 追加；不拦截调用、不包装业务函数。

用法::

    from app.core.infra.telemetry import perf_probe

    with perf_probe.span("session.switch", {"sid": sid}):
        ...

    perf_probe.count("card.paint")
    perf_probe.gauge("pool.size", n)
    perf_probe.snapshot()          # 手动记一次当前 RSS

    perf_probe.start_mem_sampler() # 显式起后台采样（关闭态返回 None）
    perf_probe.report()            # 控制台摘要（关闭态返回空 dict）
"""

from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional

_ENV_FLAG = "DRIFOX_PERF_PROBE"
_ENV_OUT = "DRIFOX_PERF_PROBE_OUT"
_ENV_MEM = "DRIFOX_PERF_PROBE_MEM"
_ENV_MEM_MS = "DRIFOX_PERF_PROBE_MEM_MS"

_DEFAULT_OUT = os.path.join(".perf_out", "perf_probe.jsonl")
_DEFAULT_MEM_MS = 2000

# 进程级聚合（关闭态下永远不会被写入）
_TOTALS: Dict[str, float] = {}
_COUNTS: Dict[str, int] = {}
_SPANS: Dict[str, list] = {}
_LOCK = threading.Lock()
_MEM_THREAD: Optional[threading.Thread] = None
_MEM_STOP = threading.Event()


def _packaged() -> bool:
    """是否运行在打包产物里（PyInstaller 等）。打包版诊断一律不可用。"""
    return bool(getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"))


def enabled() -> bool:
    """探针是否启用。打包版恒 False（环境变量也无法强开）。"""
    if _packaged():
        return False
    return os.environ.get(_ENV_FLAG, "").strip().lower() in ("1", "true", "on", "yes")


def output_path() -> str:
    """JSONL 落点（只算路径，不建目录）。"""
    return os.environ.get(_ENV_OUT, "").strip() or _DEFAULT_OUT


def _write(record: Dict[str, Any]) -> None:
    """追加一行 JSONL；任何异常静默丢弃本条（观测不得反噬主功能）。"""
    if not enabled():
        return
    try:
        import json

        record.setdefault("ts", round(time.time(), 3))
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        path = output_path()
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with _LOCK:
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
    except Exception:  # noqa: BLE001 - 观测失败必须静默
        return


def _rss_mb() -> Optional[float]:
    """当前进程 RSS（MB）；psutil 缺失返回 None。"""
    try:
        import psutil

        return round(psutil.Process().memory_info().rss / 1048576, 1)
    except Exception:  # noqa: BLE001
        return None


def _perf_row(stat, depth: int = 25) -> str:
    """把一个 profiling 统计行渲染成单行文本（file:line func → callers）。"""
    try:
        frame = stat.traceback[0]
        head = f"{os.path.basename(frame.filename)}:{frame.lineno} {stat.traceback[0].name}"
        if depth <= 1:
            return head
        chain = " <- ".join(
            f"{os.path.basename(f.filename)}:{f.lineno}" for f in stat.traceback[1:4]
        )
        return f"{head} <- {chain}" if chain else head
    except Exception:  # noqa: BLE001
        return "<unprintable>"


@contextmanager
def span(name: str, tags: Optional[Dict[str, Any]] = None):
    """计时上下文：退出时记一条 ``{"kind":"span","name","ms","tags"}``。

    关闭态直接 yield，零计时开销（不做 perf_counter 调用）。异常照常抛出，
    只在 finally 里记录耗时，不吞异常、不改控制流。
    """
    if not enabled():
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        with _LOCK:
            _SPANS.setdefault(name, []).append(elapsed_ms)
        _write({"kind": "span", "name": name, "ms": elapsed_ms, "tags": tags or {}})


def record_span(name: str, ms: float, tags: Optional[Dict[str, Any]] = None) -> None:
    """直接记一条已测好的耗时（供已有分段打点复用，避免再包一层计时）。"""
    if not enabled():
        return
    with _LOCK:
        _SPANS.setdefault(name, []).append(float(ms))
    _write({"kind": "span", "name": name, "ms": round(float(ms), 2), "tags": tags or {}})


def count(name: str, n: int = 1, tags: Optional[Dict[str, Any]] = None) -> None:
    """累加计数（只在进程内 dict，不逐次落盘；report() 时汇总）。"""
    if not enabled():
        return
    with _LOCK:
        _COUNTS[name] = _COUNTS.get(name, 0) + n
    if tags:
        _write({"kind": "count", "name": name, "n": n, "tags": tags})


def gauge(name: str, value: float, tags: Optional[Dict[str, Any]] = None) -> None:
    """记一个瞬时值（落盘）。"""
    if not enabled():
        return
    _write({"kind": "gauge", "name": name, "value": value, "tags": tags or {}})


def add(name: str, value: float, tags: Optional[Dict[str, Any]] = None) -> None:
    """累加一个量（如字节数、条数），同时落盘明细。"""
    if not enabled():
        return
    with _LOCK:
        _TOTALS[name] = _TOTALS.get(name, 0.0) + float(value)
    _write({"kind": "add", "name": name, "value": value, "tags": tags or {}})


def snapshot(tag: str = "") -> Optional[Dict[str, Any]]:
    """采样一次当前内存（RSS + 可选 tracemalloc 当前值）并落盘。"""
    if not enabled():
        return None
    row: Dict[str, Any] = {"kind": "mem", "name": tag or "snapshot", "rss_mb": _rss_mb()}
    try:
        import tracemalloc

        if tracemalloc.is_tracing():
            cur, peak = tracemalloc.get_traced_memory()
            row["tracemalloc_mb"] = round(cur / 1048576, 1)
            row["tracemalloc_peak_mb"] = round(peak / 1048576, 1)
    except Exception:  # noqa: BLE001
        pass
    _write(row)
    return row


def _mem_loop(interval_s: float) -> None:
    """后台内存采样循环（daemon）。"""
    while not _MEM_STOP.wait(interval_s):
        snapshot("auto")


def start_mem_sampler(interval_ms: Optional[int] = None) -> Optional[threading.Thread]:
    """启动后台 RSS 采样线程；未启用或未开 ``DRIFOX_PERF_PROBE_MEM`` 时返回 None。

    幂等：已启动则返回现有线程。
    """
    global _MEM_THREAD
    if not enabled():
        return None
    if os.environ.get(_ENV_MEM, "").strip().lower() not in ("1", "true", "on", "yes"):
        return None
    if _MEM_THREAD is not None and _MEM_THREAD.is_alive():
        return _MEM_THREAD
    if interval_ms is None:
        try:
            interval_ms = int(os.environ.get(_ENV_MEM_MS, str(_DEFAULT_MEM_MS)))
        except ValueError:
            interval_ms = _DEFAULT_MEM_MS
    interval_s = max(0.2, interval_ms / 1000.0)
    _MEM_STOP.clear()
    _MEM_THREAD = threading.Thread(
        target=_mem_loop, args=(interval_s,), name="perf-probe-mem", daemon=True
    )
    _MEM_THREAD.start()
    return _MEM_THREAD


def stop_mem_sampler() -> None:
    """停止后台采样线程（关闭态无操作）。"""
    _MEM_STOP.set()


def report() -> Dict[str, Any]:
    """返回进程内聚合摘要（counts/totals/span 统计），并打一份可读表格。

    关闭态返回空 dict（不打印）。
    """
    if not enabled():
        return {}
    with _LOCK:
        spans: Dict[str, Dict[str, float]] = {}
        for name, vals in _SPANS.items():
            if not vals:
                continue
            s = sorted(vals)
            mid = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
            spans[name] = {
                "n": len(s),
                "min_ms": round(s[0], 2),
                "median_ms": round(mid, 2),
                "max_ms": round(s[-1], 2),
                "sum_ms": round(sum(s), 2),
            }
        data = {
            "enabled": True,
            "output": os.path.abspath(output_path()),
            "spans": spans,
            "counts": dict(_COUNTS),
            "totals": dict(_TOTALS),
            "rss_mb": _rss_mb(),
        }

    print("\n===== PERF PROBE =====")
    print(f"output: {data['output']}")
    if spans:
        print(f"{'span':<40}{'n':>6}{'median':>10}{'min':>10}{'max':>10}")
        for name, st in sorted(spans.items(), key=lambda kv: -kv[1]["sum_ms"]):
            print(
                f"{name:<40}{st['n']:>6}{st['median_ms']:>10.2f}"
                f"{st['min_ms']:>10.2f}{st['max_ms']:>10.2f}"
            )
    if _COUNTS:
        print("counts:", dict(_COUNTS))
    if _TOTALS:
        print("totals:", {k: round(v, 2) for k, v in _TOTALS.items()})
    if data["rss_mb"] is not None:
        print(f"rss_mb: {data['rss_mb']}")
    print("===== END PROBE =====\n", flush=True)
    return data


__all__ = [
    "add",
    "count",
    "enabled",
    "gauge",
    "output_path",
    "record_span",
    "report",
    "snapshot",
    "span",
    "start_mem_sampler",
    "stop_mem_sampler",
]
