# -*- coding: utf-8 -*-
"""
锁屏远程（Lock Screen Remote）核心引擎

目标：开启后即使锁屏，电脑也不进入休眠、屏幕也不自动关闭，
同时保持 Gateway / 本地 API 在线，便于手机远程操控与自动化任务持续运行。

原理（Windows）：
- 调用 PowerCreateRequest + PowerSetRequest(
      PowerRequestSystemRequired | PowerRequestDisplayRequired)
  向系统声明「系统 / 显示器需要保持工作」，从而抑制休眠与熄屏。
  Power* API 比 SetThreadExecutionState 更稳健，能跨会话 / 锁屏持续生效。
- 回退方案：SetThreadExecutionState(
      ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)。
- LockWorkStation() 立即锁屏（可选）。
- 后台定时器周期性重新声明请求，防止被电源策略短暂清除。

非 Windows 平台：记录警告并提供 no-op 占位（可后续接入 caffeinate / systemd-inhibit）。
"""
from __future__ import annotations

import sys
import threading
from typing import Dict, Optional

from loguru import logger


# ───────────────────────── Windows 电源请求类型 ─────────────────────────
# POWER_REQUEST_TYPE 枚举（winnt.h）
_POWER_REQUEST_DISPLAY_REQUIRED = 0
_POWER_REQUEST_SYSTEM_REQUIRED = 1
# SetThreadExecutionState 标志
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002


class _WindowsPower:
    """封装 Windows 电源抑制 API；非 Windows 平台下 available=False。"""

    def __init__(self):
        self.available = False
        self._kernel32 = None
        self._user32 = None
        self._ctypes = None
        self._reason_ctx = None
        self._power_handle = None
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            self._ctypes = ctypes
            self._wintypes = wintypes
            self._kernel32 = ctypes.windll.kernel32
            self._user32 = ctypes.windll.user32

            # REASON_CONTEXT（简单字符串形式）
            class REASON_CONTEXT(ctypes.Structure):
                _fields_ = [
                    ("Version", wintypes.ULONG),
                    ("Flags", wintypes.DWORD),
                    ("SimpleReasonString", wintypes.LPCWSTR),
                ]

            self._REASON_CONTEXT = REASON_CONTEXT

            # 声明函数签名
            self._kernel32.PowerCreateRequest.argtypes = [ctypes.POINTER(REASON_CONTEXT)]
            self._kernel32.PowerCreateRequest.restype = wintypes.HANDLE
            self._kernel32.PowerSetRequest.argtypes = [wintypes.HANDLE, wintypes.ULONG]
            self._kernel32.PowerSetRequest.restype = wintypes.BOOL
            self._kernel32.PowerClearRequest.argtypes = [wintypes.HANDLE, wintypes.ULONG]
            self._kernel32.PowerClearRequest.restype = wintypes.BOOL
            self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            self._kernel32.CloseHandle.restype = wintypes.BOOL
            self._kernel32.SetThreadExecutionState.argtypes = [wintypes.ULONG]
            self._kernel32.SetThreadExecutionState.restype = wintypes.ULONG

            self.available = True
        except Exception as e:  # pragma: no cover - 仅在异常环境
            logger.warning(f"[LockScreenRemote] Windows 电源 API 初始化失败: {e}")
            self.available = False

    # ---- Power* API ----
    def create_request(self) -> bool:
        if not self.available or self._power_handle is not None:
            return self._power_handle is not None
        try:
            ctx = self._REASON_CONTEXT()
            ctx.Version = 0  # POWER_REQUEST_CONTEXT_VERSION
            ctx.Flags = 0x1  # POWER_REQUEST_CONTEXT_SIMPLE_STRING
            ctx.SimpleReasonString = "DriFox 锁屏远程：保持系统唤醒与显示器常亮"
            handle = self._kernel32.PowerCreateRequest(self._ctypes.byref(ctx))
            if not handle:
                logger.warning("[LockScreenRemote] PowerCreateRequest 返回空句柄")
                return False
            self._reason_ctx = ctx  # 保活，避免被 GC 回收
            self._power_handle = handle
            return True
        except Exception as e:
            logger.error(f"[LockScreenRemote] PowerCreateRequest 异常: {e}")
            return False

    def set_request(self, request_type: int) -> bool:
        if not self.available or self._power_handle is None:
            return False
        try:
            return self._kernel32.PowerSetRequest(self._power_handle, request_type) != 0
        except Exception as e:
            logger.error(f"[LockScreenRemote] PowerSetRequest 异常: {e}")
            return False

    def clear_request(self, request_type: int) -> bool:
        if not self.available or self._power_handle is None:
            return False
        try:
            return self._kernel32.PowerClearRequest(self._power_handle, request_type) != 0
        except Exception as e:
            logger.error(f"[LockScreenRemote] PowerClearRequest 异常: {e}")
            return False

    def close_request(self) -> None:
        if self._power_handle is not None:
            try:
                self._kernel32.CloseHandle(self._power_handle)
            except Exception:
                pass
            self._power_handle = None
            self._reason_ctx = None

    # ---- SetThreadExecutionState 回退 ----
    def set_thread_execution(self, flags: int) -> bool:
        if not self.available:
            return False
        try:
            prev = self._kernel32.SetThreadExecutionState(flags)
            return prev != 0
        except Exception as e:
            logger.error(f"[LockScreenRemote] SetThreadExecutionState 异常: {e}")
            return False

    # ---- 锁屏 ----
    def lock_workstation(self) -> bool:
        if not self.available or self._user32 is None:
            return False
        try:
            return self._user32.LockWorkStation() != 0
        except Exception as e:
            logger.warning(f"[LockScreenRemote] 锁屏调用失败: {e}")
            return False


class LockScreenRemoteManager:
    """锁屏远程管理器（进程内单例）。"""

    def __init__(self):
        self._win = _WindowsPower()
        self._enabled = False
        self._lock_now = True
        self._keep_display_on = True
        self._reassert_timer: Optional[threading.Timer] = None
        self._reassert_interval = 30  # 秒
        self._lock = threading.Lock()

    # ---- 公共接口 ----
    def enable(self, lock_now: bool = False, keep_display_on: bool = True) -> Dict[str, object]:
        """开启锁屏远程。

        Args:
            lock_now: 是否立即锁屏
            keep_display_on: 是否保持显示器常亮（否则仅阻止系统休眠）
        """
        with self._lock:
            first_time = not self._enabled
            self._enabled = True
            self._lock_now = lock_now
            self._keep_display_on = keep_display_on
            self._apply_power(True)
            if lock_now:
                self._win.lock_workstation()
            self._start_reassert()
            if first_time:
                logger.info("[LockScreenRemote] 已启用")
            return self.status()

    def disable(self) -> Dict[str, object]:
        """关闭锁屏远程，恢复系统正常休眠策略。"""
        with self._lock:
            if not self._enabled:
                return self.status()
            self._enabled = False
            self._stop_reassert()
            self._apply_power(False)
            logger.info("[LockScreenRemote] 已关闭")
            return self.status()

    def toggle(self) -> Dict[str, object]:
        """切换开关状态。"""
        return self.disable() if self._enabled else self.enable()

    def is_enabled(self) -> bool:
        return self._enabled

    def status(self) -> Dict[str, object]:
        return {
            "enabled": self._enabled,
            "platform_windows": self._win.available,
            "lock_now": self._lock_now,
            "keep_display_on": self._keep_display_on,
            "power_request_active": (
                self._win._power_handle is not None if self._win.available else False
            ),
        }

    # ---- 内部实现 ----
    def _apply_power(self, on: bool) -> bool:
        """开启 / 关闭电源抑制。优先 Power* API，失败回退 SetThreadExecutionState。"""
        if not self._win.available:
            logger.warning("[LockScreenRemote] 非 Windows 平台或 API 不可用，仅切换内存状态")
            return False
        if on:
            if self._win._power_handle is None:
                if self._win.create_request():
                    ok_sys = self._win.set_request(_POWER_REQUEST_SYSTEM_REQUIRED)
                    ok_disp = True
                    if self._keep_display_on:
                        ok_disp = self._win.set_request(_POWER_REQUEST_DISPLAY_REQUIRED)
                    return ok_sys and ok_disp
                # Power* 失败 → 回退
                logger.warning("[LockScreenRemote] Power* API 失败，回退 SetThreadExecutionState")
                return self._win.set_thread_execution(
                    _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED
                )
            # 句柄已存在，再次声明（幂等）
            self._win.set_request(_POWER_REQUEST_SYSTEM_REQUIRED)
            if self._keep_display_on:
                self._win.set_request(_POWER_REQUEST_DISPLAY_REQUIRED)
            return True
        else:
            if self._win._power_handle is not None:
                self._win.clear_request(_POWER_REQUEST_SYSTEM_REQUIRED)
                if self._keep_display_on:
                    self._win.clear_request(_POWER_REQUEST_DISPLAY_REQUIRED)
                self._win.close_request()
            else:
                # 回退分支：清除执行状态
                self._win.set_thread_execution(_ES_CONTINUOUS)
            return True

    def _start_reassert(self) -> None:
        self._stop_reassert()

        def _tick() -> None:
            if not self._enabled:
                return
            try:
                self._apply_power(True)
            except Exception as e:  # pragma: no cover
                logger.warning(f"[LockScreenRemote] 重新声明电源请求失败: {e}")
            if self._enabled:
                self._reassert_timer = threading.Timer(self._reassert_interval, _tick)
                self._reassert_timer.daemon = True
                self._reassert_timer.start()

        self._reassert_timer = threading.Timer(self._reassert_interval, _tick)
        self._reassert_timer.daemon = True
        self._reassert_timer.start()

    def _stop_reassert(self) -> None:
        if self._reassert_timer is not None:
            self._reassert_timer.cancel()
            self._reassert_timer = None


# ───────────────────────── 单例 ─────────────────────────
_manager_instance: Optional[LockScreenRemoteManager] = None


def get_lock_screen_remote_manager() -> LockScreenRemoteManager:
    """获取锁屏远程管理器单例。"""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = LockScreenRemoteManager()
    return _manager_instance
