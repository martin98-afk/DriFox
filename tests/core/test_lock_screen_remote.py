# -*- coding: utf-8 -*-
"""
锁屏远程引擎单元测试

覆盖：
- 单例唯一性
- enable / disable 切换内存状态与 status 字段
- 非 Windows 平台安全降级（不抛异常，仅切换内存状态）
"""
import importlib
import sys

import pytest


def _import_manager():
    """导入引擎模块（隔离单例，避免测试间相互污染）。"""
    import app.core.system.lock_screen_remote as mod

    # 重置模块级单例，保证每个测试独立
    mod._manager_instance = None
    importlib.reload(mod)
    return mod


def test_singleton_is_unique():
    mod = _import_manager()
    a = mod.get_lock_screen_remote_manager()
    b = mod.get_lock_screen_remote_manager()
    assert a is b
    assert isinstance(a, mod.LockScreenRemoteManager)


def test_enable_disable_toggles_state():
    mod = _import_manager()
    mgr = mod.get_lock_screen_remote_manager()

    assert mgr.is_enabled() is False
    status_off = mgr.status()
    assert status_off["enabled"] is False

    # enable 不应抛异常（非 Windows 下仅切换内存状态）
    s_on = mgr.enable(lock_now=False, keep_display_on=True)
    assert s_on["enabled"] is True
    assert mgr.is_enabled() is True
    assert s_on["keep_display_on"] is True

    # 再次 enable 为幂等
    s_on2 = mgr.enable(lock_now=False, keep_display_on=False)
    assert s_on2["enabled"] is True
    assert s_on2["keep_display_on"] is False

    s_off = mgr.disable()
    assert s_off["enabled"] is False
    assert mgr.is_enabled() is False

    # disable 二次调用安全
    s_off2 = mgr.disable()
    assert s_off2["enabled"] is False


def test_toggle():
    mod = _import_manager()
    mgr = mod.get_lock_screen_remote_manager()
    assert mgr.is_enabled() is False
    mgr.toggle()
    assert mgr.is_enabled() is True
    mgr.toggle()
    assert mgr.is_enabled() is False


def test_status_keys_present():
    mod = _import_manager()
    mgr = mod.get_lock_screen_remote_manager()
    keys = set(mgr.status().keys())
    assert {
        "enabled",
        "platform_windows",
        "lock_now",
        "keep_display_on",
        "power_request_active",
    } <= keys


def test_reassert_timer_stopped_after_disable():
    mod = _import_manager()
    mgr = mod.get_lock_screen_remote_manager()
    mgr.enable(lock_now=False)
    assert mgr._reassert_timer is not None
    mgr.disable()
    assert mgr._reassert_timer is None
