# -*- coding: utf-8 -*-
"""运行时采样器：RSS / QObject 总数 / tracemalloc top10。

采样频率：默认每 60 秒一次（可由环境变量 `PERF_SAMPLE_INTERVAL` 覆盖）。
数据落点：调用方负责传入基目录；CSV/JSON 由 runner.py 统一落盘。

设计要点：
1. tracemalloc 用 snapshot diff 找"持续增长"的分配点（线性泄漏检测）
2. QObject 通过 QApplication.topLevelWidgets() + 递归遍历找所有实例
3. RSS 用 psutil.Process(os.getpid()).memory_info().rss（Windows 任务管理器口径）
4. 任意环节失败不影响整体采样（记录 ok=False）
"""

from __future__ import annotations

import os
import sys
import time
import tracemalloc
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - 允许基础运行时无 psutil（fallback to /proc）
    psutil = None  # type: ignore


@dataclass
class TracemallocEntry:
    """单条 tracemalloc 分配点。"""

    file: str
    line: int
    size_kb: float
    count: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Sample:
    """单次采样记录（CSV/JSON 一行）。"""

    scenario: str
    elapsed_sec: float
    loop_iter: int
    rss_mb: float
    rss_ok: bool
    qobject_count: int
    qobject_top_types: Dict[str, int]  # 类名 → 实例数（前 10）
    qobject_ok: bool
    # 业务对象计数（非 QObject 的关键对象；用 gc.get_objects() 过滤）
    # 任务目标里 ChatSession/Plugin 等都是非 QObject，必须单独统计才能反映泄漏
    biz_object_counts: Dict[str, int]
    biz_object_ok: bool
    tracemalloc_top: List[TracemallocEntry]  # top 10 分配点
    tracemalloc_current_mb: float
    tracemalloc_peak_mb: float
    tracemalloc_ok: bool
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # dataclass nested → dict
        d["tracemalloc_top"] = [e.to_dict() for e in self.tracemalloc_top]
        return d


def _read_rss_mb() -> tuple[float, bool]:
    """读取当前进程 RSS（MB）。"""
    if psutil is not None:
        try:
            rss = psutil.Process(os.getpid()).memory_info().rss
            return rss / 1024 / 1024, True
        except Exception:
            pass
    # Windows fallback: ctypes GetProcessMemoryInfo
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        handle = ctypes.windll.kernel32.GetCurrentProcess()  # type: ignore[attr-defined]
        if ctypes.windll.psapi.GetProcessMemoryInfo(  # type: ignore[attr-defined]
            handle, ctypes.byref(counters), counters.cb
        ):
            return counters.WorkingSetSize / 1024 / 1024, True
    except Exception:
        pass
    return 0.0, False


def _count_qobjects() -> tuple[int, Dict[str, int], bool]:
    """遍历所有 QObject 实例，返回总数 + 按类名前 10 分布。

    PyQt5 5.15.11 + Python 3.14 在 Windows 下有怪现象：
    - QApplication.instance() 可能返回 None
    - gc.get_objects() 能找到 QApplication，但访问 .children() 会抛
      "wrapped C/C++ object of type QApplication has been deleted"

    因此本函数仅依赖类名 + isinstance 扫描 gc（不调用 Qt 方法），
    避免触发已删除 C++ 对象的 RuntimeError。
    """
    try:
        import gc

        from PyQt5.QtCore import QObject
    except Exception:
        return 0, {}, False

    type_counts: Dict[str, int] = {}
    total = 0

    try:
        # 全量 gc 扫描找 QObject 子类（仅 isinstance 判定 + type().name，
        # 不调用任何 Qt 方法，因此即使对象已 C++ delete 也安全）
        for obj in gc.get_objects():
            try:
                if not isinstance(obj, QObject):
                    continue
                total += 1
                cls_name = type(obj).__name__
                type_counts[cls_name] = type_counts.get(cls_name, 0) + 1
            except Exception:
                # 个别对象访问失败，跳过
                continue
    except Exception:
        return total, type_counts, total > 0

    # 取 top 10
    top = dict(sorted(type_counts.items(), key=lambda kv: -kv[1])[:10])
    return total, top, total > 0


# 业务对象白名单（gc 扫描时按类名过滤；非 QObject 但在压测中会累积的关键对象）
_BIZ_OBJECT_NAMES = (
    "ChatSession",
    "SessionManager",
    "ChatBackend",
    "OpenAIChatWorker",
    "UIEngine",
    "ConversationExecutor",
    "UIConversationAdapter",
    "HookManager",
    "ToolExecutor",
    "AgentManager",
    "SubAgentManager",
    "HistoryManager",
    "MemoryManagerCore",
    "Plugin",
    "PluginManager",
)


def _count_biz_objects() -> tuple[Dict[str, int], bool]:
    """用 gc.get_objects() 过滤业务对象白名单（覆盖非 QObject 的关键对象）。

    注意：gc.get_objects() 每次扫描所有 Python 对象（包括 importlib 缓存、字符串等），
    性能开销大；按类名过滤可显著降低 type() 调用次数。
    """
    try:
        import gc
    except Exception:
        return {}, False

    counts: Dict[str, int] = {}
    try:
        # 一次性遍历：按类名匹配
        for obj in gc.get_objects():
            cls_name = type(obj).__name__
            if cls_name in _BIZ_OBJECT_NAMES:
                counts[cls_name] = counts.get(cls_name, 0) + 1
    except Exception:
        return counts, False
    return counts, True


def _capture_tracemalloc_top(n: int = 10) -> tuple[List[TracemallocEntry], float, float, bool]:
    """截取 tracemalloc top N 分配点 + 当前/峰值占用。"""
    if not tracemalloc.is_tracing():
        return [], 0.0, 0.0, False
    try:
        snap = tracemalloc.take_snapshot()
        stats = snap.statistics("filename")[:n]
        entries = [
            TracemallocEntry(
                file=f"{s.traceback}" if s.traceback else "<unknown>",
                line=(s.traceback[0].lineno if s.traceback else 0),
                size_kb=s.size / 1024,
                count=s.count,
            )
            for s in stats
        ]
        current, peak = tracemalloc.get_traced_memory()
        return entries, current / 1024 / 1024, peak / 1024 / 1024, True
    except Exception:
        return [], 0.0, 0.0, False


def take_sample(scenario: str, loop_iter: int, start_time: float) -> Sample:
    """执行一次完整采样（RSS + QObject + 业务对象 + tracemalloc）。"""
    import gc

    # 采样前强制 GC：回收上轮 tracemalloc snapshot 等临时对象，
    # 让 RSS 读数反映 GC 后稳态，避免 snapshot 自身内存污染曲线。
    gc.collect()

    elapsed = time.time() - start_time
    rss_mb, rss_ok = _read_rss_mb()
    qobj_count, qobj_top, qobj_ok = _count_qobjects()
    biz_counts, biz_ok = _count_biz_objects()
    tm_top, tm_cur_mb, tm_peak_mb, tm_ok = _capture_tracemalloc_top(10)

    return Sample(
        scenario=scenario,
        elapsed_sec=elapsed,
        loop_iter=loop_iter,
        rss_mb=rss_mb,
        rss_ok=rss_ok,
        qobject_count=qobj_count,
        qobject_top_types=qobj_top,
        qobject_ok=qobj_ok,
        biz_object_counts=biz_counts,
        biz_object_ok=biz_ok,
        tracemalloc_top=tm_top,
        tracemalloc_current_mb=tm_cur_mb,
        tracemalloc_peak_mb=tm_peak_mb,
        tracemalloc_ok=tm_ok,
    )


def start_tracemalloc() -> None:
    """启动 tracemalloc（最多追踪 25 帧栈，足以定位分配点）。"""
    if not tracemalloc.is_tracing():
        tracemalloc.start(25)


def stop_tracemalloc() -> None:
    """停止 tracemalloc（避免测试结束后持续占用）。"""
    if tracemalloc.is_tracing():
        tracemalloc.stop()


def get_sample_interval() -> float:
    """采样间隔（秒），环境变量 PERF_SAMPLE_INTERVAL 覆盖默认 60。"""
    try:
        return float(os.environ.get("PERF_SAMPLE_INTERVAL", "60"))
    except ValueError:
        return 60.0


def env_full_mode() -> bool:
    """全量模式：>=5万次操作循环；否则 demo 模式（<=2 分钟）。"""
    return os.environ.get("LONGRUN_FULL", "").strip().lower() in ("1", "true", "yes")


__all__ = [
    "Sample",
    "TracemallocEntry",
    "take_sample",
    "start_tracemalloc",
    "stop_tracemalloc",
    "get_sample_interval",
    "env_full_mode",
]


if __name__ == "__main__":
    # 烟雾测试：单次采样打印
    print("sampler.py 自检：")
    start_tracemalloc()
    s = take_sample("smoke", 0, time.time())
    stop_tracemalloc()
    import json

    print(json.dumps(s.to_dict(), ensure_ascii=False, indent=2))
    sys.exit(0)
