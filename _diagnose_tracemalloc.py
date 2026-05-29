# -*- coding: utf-8 -*-
"""
深度内存诊断：用 tracemalloc 追踪单步内存分配热点。
在 chat_worker 的 _mem_snapshot 中触发，定位 +258MB 这样的异常分配来自哪里。

使用方法：
  set MEM_TRACE=1
  python main.py
  # 在 mem_diag.log 中看到 [TRACE] 日志，包含分配栈
"""

import os
import sys
import gc

# ============================================================
# Tracemalloc 诊断工具（在 ChatWorker 中按需启用）
# ============================================================

_tracemalloc_enabled = os.environ.get('MEM_TRACE') == '1'
_tracemalloc_snapshot = None  # 上一次的快照

def mem_trace_start():
    """在 before_api_call 调用：启动 tracemalloc 追踪"""
    if not _tracemalloc_enabled:
        return
    import tracemalloc
    if not tracemalloc.is_tracing():
        tracemalloc.start(25)  # 记录 25 层调用栈
    global _tracemalloc_snapshot
    gc.collect()
    _tracemalloc_snapshot = tracemalloc.take_snapshot()


def mem_trace_stop(step_name: str, threshold_mb: float = 10.0):
    """
    在 after_api_call 调用：对比内存分配变化，输出超过 threshold_mb 的热点。

    结果写入 log_dir / "mem_trace.log"
    """
    if not _tracemalloc_enabled or _tracemalloc_snapshot is None:
        return
    import tracemalloc

    gc.collect()
    new_snapshot = tracemalloc.take_snapshot()

    # 计算 vs 基准的差值
    diff = new_snapshot.compare_to(_tracemalloc_snapshot, 'lineno')

    # 按分配大小降序排列
    diff = [d for d in diff if d.size_diff > threshold_mb * 1024 * 1024]
    diff.sort(key=lambda d: d.size_diff, reverse=True)

    if not diff:
        return

    # 写入日志
    log_dir = None
    try:
        from app.utils.utils import get_app_data_dir
        log_dir = get_app_data_dir() / "logs"
    except Exception:
        log_dir = None

    if log_dir:
        trace_path = str(log_dir / "mem_trace.log")
    else:
        trace_path = "mem_trace.log"

    with open(trace_path, 'a', encoding='utf-8') as f:
        f.write(f"\n{'='*80}\n")
        f.write(f"[TRACE] {step_name} — 分配 > {threshold_mb:.0f}MB 的热点:\n")
        f.write(f"{'='*80}\n")
        f.write(f"{'大小(增量)':>12} {'总大小':>10} {'计数':>6} {'文件:行号':<50} 调用栈\n")
        f.write(f"{'-'*80}\n")

        for stat in diff[:20]:  # 输出前 20 个热点
            size_mb = stat.size_diff / 1024 / 1024
            total_mb = stat.size / 1024 / 1024
            count = stat.count_diff
            # 取最内层的框架（最有意义的分配点）
            frame = stat.traceback[0]
            location = f"{frame.filename.split('site-packages')[-1] if 'site-packages' in frame.filename else frame.filename}:{frame.lineno}"
            f.write(f"{size_mb:>8.1f}MB  {total_mb:>8.1f}MB  {count:>5}  {location:<50}\n")

            # 输出调用栈（缩短到 3 层）
            for i, frame in enumerate(stat.traceback[:5]):
                short = f"  ├─ {frame.filename.split('site-packages')[-1] if 'site-packages' in frame.filename else frame.filename}:{frame.lineno} in {frame.name}"
                f.write(f"{short}\n")

        f.write(f"\n")

    # 更新基准照
    global _tracemalloc_snapshot
    _tracemalloc_snapshot = new_snapshot

    # 也输出到 logger
    from loguru import logger
    logger.warning(f"[MEM-TRACE] 写入 {trace_path}，发现 {len(diff)} 个 >{threshold_mb:.0f}MB 热点")


# ============================================================
# Windows 堆压缩工具（cleanup 后主动缩容 RSS）
# ============================================================

def compact_windows_heap():
    """
    在 cleanup() 后调用。Windows 下强制压缩进程堆，
    让 Python 分配器将空闲 arena 归还给 OS。

    仅在 Windows + cleanup 时可用。
    """
    if sys.platform != 'win32':
        return

    try:
        import ctypes
        from ctypes import wintypes

        # 获取默认进程堆句柄
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        GetProcessHeap = kernel32.GetProcessHeap
        GetProcessHeap.restype = wintypes.HANDLE

        # 压缩堆（HeapCompact）
        HeapCompact = kernel32.HeapCompact
        HeapCompact.restype = ctypes.c_size_t
        HeapCompact.argtypes = [wintypes.HANDLE, wintypes.DWORD]

        heap = GetProcessHeap()
        if heap:
            freed = HeapCompact(heap, 0)
            if freed > 0:
                from loguru import logger
                logger.info(f"[MEM-HEAP] Windows 堆压缩释放了 {freed / 1024 / 1024:.1f} MB")
            return freed
    except Exception as e:
        from loguru import logger
        logger.debug(f"[MEM-HEAP] 堆压缩失败: {e}")
        return 0


def compact_heap_safe():
    """安全版本的堆压缩，跨平台兼容"""
    # 1. 强制 Python 级 GC
    before = len(gc.get_objects())
    gc.collect()
    after = len(gc.get_objects())

    # 2. Windows 堆压缩
    freed = compact_windows_heap()

    return {
        "gc_objects_freed": before - after,
        "heap_compact_freed": freed or 0,
    }


if __name__ == "__main__":
    # 测试堆压缩
    print("测试堆压缩...")
    result = compact_heap_safe()
    print(f"  结果: {result}")
