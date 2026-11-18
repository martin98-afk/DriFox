# -*- coding: utf-8 -*-
"""一次性启动诊断打点（测完整体删除）。

DRIFOX_STARTUP_PROBE=1 时启用：
- mark()：记录分段耗时（原有行为）
- mark_mem()：记录该时刻真实物理占用（darwin 读 proc_pid_rusage.phys_footprint，
  避开 RSS 虚高；其它平台返回 0）
- dump()：打印分段耗时表 + footprint 表

trace_enabled() / footprint_mb()：轻量入口，给既有启动日志行追加 ``fp=…MB``
后缀。源码运行默认开（不新增日志行），打包版默认关。

DRIFOX_STARTUP_PROBE_EXIT_MS=N：dump 后 N 毫秒强制退出（os._exit，绕过
QtWebEngine teardown 挂起）。用于无人值守跑一次启动内存曲线。
"""

import os
import sys
import time

_ENABLED = os.environ.get("DRIFOX_STARTUP_PROBE") == "1"
if _ENABLED:
    # 诊断模式自带 faulthandler：启动早期（壳显示前，正式崩溃捕获尚未安装）
    # 发生原生崩溃时也能把 Python 栈落到 stderr。
    try:
        import faulthandler

        faulthandler.enable()
    except Exception:
        pass
_T0 = time.perf_counter()
_SEG = _T0
_ROWS: list = []
_MEM_ROWS: list = []
_LIBC = None
_INIT_TRIED = False
_EXIT_SCHEDULED = False

# proc_pid_rusage 的接收缓冲。**必须放大**：系统按 flavor 对应的完整结构体写入，
# 缓冲区不足会越界写、破坏堆——2026-11-18 实测按 240 字节（rusage_info_v4 名义
# 大小）接收，调用本身返回正确数值，但堆已被写坏，下一次大 import 才 segfault，
# 表现为「启动必崩」且极难归因。故留 1KB 余量，只按偏移取需要的字段。
_BUF_SIZE = 1024
# ri_phys_footprint 偏移：ri_uuid[16] + 前 7 个 uint64
_FP_OFFSET = 16 + 7 * 8


def _init_footprint() -> bool:
    """惰性初始化 darwin footprint 读取所需的 libc 句柄与函数签名。"""
    global _LIBC, _INIT_TRIED
    if _INIT_TRIED:
        return _LIBC is not None
    _INIT_TRIED = True
    try:
        import ctypes

        # 固定路径而非 ctypes.util.find_library：后者在已有多线程的进程里
        # 可能走子进程探测，Qt 进程内不值得冒这个险。
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        # 显式声明签名：不声明时 ctypes 按 c_int 转换，指针参数行为不保证。
        lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        lib.proc_pid_rusage.restype = ctypes.c_int
        _LIBC = lib
    except Exception:
        _LIBC = None
    return _LIBC is not None


def _footprint_mb() -> float:
    """当前进程 phys_footprint（MB）；非 macOS 或读取失败返回 0。"""
    if sys.platform != "darwin" or not _init_footprint():
        return 0.0
    try:
        import ctypes

        buf = ctypes.create_string_buffer(_BUF_SIZE)
        if _LIBC.proc_pid_rusage(os.getpid(), 4, buf) != 0:  # type: ignore[union-attr]
            return 0.0
        return ctypes.c_uint64.from_buffer(buf, _FP_OFFSET).value / 1048576
    except Exception:
        return 0.0


def mark(label: str) -> None:
    """打一个分段点：记录上一段耗时。"""
    global _SEG
    if not _ENABLED:
        return
    now = time.perf_counter()
    _ROWS.append((label, (now - _SEG) * 1000, (now - _T0) * 1000))
    _SEG = now


def mark_mem(label: str) -> None:
    """记录该时刻 footprint（MB）。未启用探针时零开销直接返回。"""
    if not _ENABLED:
        return
    _MEM_ROWS.append((label, _footprint_mb(), (time.perf_counter() - _T0) * 1000))


def trace_enabled() -> bool:
    """轻量打点开关：源码运行默认开（启动日志直接带内存数字），打包版默认关。

    与 mark()/dump() 的 ``DRIFOX_STARTUP_PROBE=1`` 独立：那条路径会打整张表，
    这条只给既有日志行追加一个 ``fp=…MB`` 后缀，零新增日志行。
    显式 ``DRIFOX_STARTUP_PROBE=0`` 可关。
    """
    if _ENABLED:
        return True
    raw = os.environ.get("DRIFOX_STARTUP_PROBE", "").strip().lower()
    if raw in ("0", "false", "no"):
        return False
    return not (getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"))


def footprint_mb() -> float:
    """公开入口：当前进程 phys_footprint（MB）；非 macOS 或失败返回 0。"""
    return _footprint_mb()


def _maybe_schedule_exit() -> None:
    """按 DRIFOX_STARTUP_PROBE_EXIT_MS 安排一次强制退出（仅探针模式）。"""
    global _EXIT_SCHEDULED
    if _EXIT_SCHEDULED:
        return
    _EXIT_SCHEDULED = True
    try:
        ms = int(os.environ.get("DRIFOX_STARTUP_PROBE_EXIT_MS", "0"))
    except ValueError:
        return
    if ms <= 0:
        return
    import threading

    threading.Timer(ms / 1000.0, lambda: os._exit(0)).start()


def dump() -> None:
    """输出采集结果（启动末尾调用一次）。"""
    if not _ENABLED:
        return
    print("\n===== STARTUP PROBE =====")
    print(f"{'label':<40}{'seg_ms':>10}{'cum_ms':>10}")
    for label, seg, cum in _ROWS:
        print(f"{label:<40}{seg:>10.1f}{cum:>10.1f}")
    if _MEM_ROWS:
        print("\n----- FOOTPRINT (MB) -----")
        print(f"{'label':<40}{'fp_mb':>10}{'delta':>10}{'since_t0':>10}")
        base = _MEM_ROWS[0][1]
        prev = base
        for label, mb, cum in _MEM_ROWS:
            print(f"{label:<40}{mb:>10.1f}{mb - prev:>+10.1f}{mb - base:>+10.1f}")
            prev = mb
    print("===== END PROBE =====\n", flush=True)
    _maybe_schedule_exit()
