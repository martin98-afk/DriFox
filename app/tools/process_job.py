# -*- coding: utf-8 -*-
"""
Windows Job Object 进程树管理 (S3)

提供「创建 → kill-on-close → 子进程入 Job」的进程树容器，用于：
- 一键杀灭命令及其全部子进程（避免后台任务残留）
- command_safety.run_safe / run_with_shell 接入点（可选 job 参数）

非 Windows 平台：is_supported() 返回 False，assign/kill 均为安全 no-op，
不影响 Linux/macOS 构建与运行。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes as wt
from typing import Optional

from loguru import logger

# ============================================================
# Windows 常量与结构
# ============================================================
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9

PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001

ERROR_ACCESS_DENIED = 5


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wt.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wt.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _load_kernel32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wt.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    k32.SetInformationJobObject.restype = wt.BOOL
    k32.SetInformationJobObject.argtypes = [
        wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
    ]
    k32.AssignProcessToJobObject.restype = wt.BOOL
    k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    k32.TerminateJobObject.restype = wt.BOOL
    k32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.CloseHandle.restype = wt.BOOL
    k32.CloseHandle.argtypes = [wt.HANDLE]
    return k32


_kernel32 = _load_kernel32() if sys.platform == "win32" else None


class ProcessJob:
    """Windows Job Object 进程树容器。

    用法::

        with ProcessJob() as job:
            proc = run_safe("ping -n 10 127.0.0.1", job=job)
            ...
        # 退出 with 时关闭 Job 句柄 → kill-on-close 杀灭全部进程树

    非 Windows 平台：所有操作均为安全 no-op（is_supported() == False）。
    """

    __slots__ = ("_handle", "_closed")

    def __init__(self, name: Optional[str] = None):
        self._handle = None
        self._closed = False
        if _kernel32 is None:
            return
        handle = _kernel32.CreateJobObjectW(None, name or None)
        if not handle:
            err = ctypes.get_last_error()
            raise OSError(err, f"CreateJobObjectW failed: {err}")
        # 配置 kill-on-close：Job 句柄关闭时自动终止所有关联进程
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            handle, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        )
        if not ok:
            _kernel32.CloseHandle(handle)
            err = ctypes.get_last_error()
            raise OSError(err, f"SetInformationJobObject failed: {err}")
        self._handle = handle

    @staticmethod
    def is_supported() -> bool:
        """当前平台是否支持 Job Object（仅 Windows）"""
        return _kernel32 is not None

    def assign(self, pid: int) -> bool:
        """把进程（及其未来子进程）加入 Job。

        失败（如进程已退出、已在其他 Job 且未启用嵌套）返回 False，
        不抛出异常 —— 调用方据此决定是否降级。
        """
        if self._closed or _kernel32 is None or self._handle is None:
            return False
        h_process = _kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
        if not h_process:
            err = ctypes.get_last_error()
            if err != ERROR_ACCESS_DENIED:
                logger.debug(f"[ProcessJob] OpenProcess({pid}) failed: {err}")
            return False
        try:
            ok = _kernel32.AssignProcessToJobObject(self._handle, h_process)
            if not ok:
                err = ctypes.get_last_error()
                # ERROR_ACCESS_DENIED: 进程已在其他 Job 且未启用嵌套 Job
                logger.warning(
                    f"[ProcessJob] AssignProcessToJobObject({pid}) failed: {err}"
                    " (进程可能已在其他 Job 中)"
                )
                return False
            return True
        finally:
            _kernel32.CloseHandle(h_process)

    def kill(self, exit_code: int = 1) -> bool:
        """立即终止 Job 内全部进程树。返回是否成功（非 Windows 恒 False）。"""
        if self._closed or _kernel32 is None or self._handle is None:
            return False
        ok = _kernel32.TerminateJobObject(self._handle, exit_code)
        if not ok:
            err = ctypes.get_last_error()
            logger.warning(f"[ProcessJob] TerminateJobObject failed: {err}")
        return bool(ok)

    def close(self) -> None:
        """关闭 Job 句柄 → kill-on-close 生效（杀灭全部进程树）。幂等。"""
        if self._closed:
            return
        self._closed = True
        if _kernel32 is not None and self._handle is not None:
            _kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "ProcessJob":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
