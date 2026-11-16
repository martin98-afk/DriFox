# -*- coding: utf-8 -*-
"""热键健康检查重试异步化 回归测试。

背景：_health_check_hotkey 非 Windows 分支在 add_hotkey 失败重试时同步
``time.sleep(1)``，最长阻塞主线程 ~2s（槽挂在 5min 周期的主线程 QTimer 上）。

修复：重试链改 QTimer.singleShot(1000, ...) 异步串行，加 _hotkey_retrying
互斥防周期 timer 重入；成功/失败终态语义不变。

打桩方式：monkeypatch ``app.tray_manager.platform/keyboard``；TrayManager 用
``object.__new__`` 跳过重构造（本测试只驱动 _health_check_hotkey 路径），
实例属性手工补齐该方法依赖的最小状态集。

验证：
1. add_hotkey 连续抛 2 次异常第 3 次成功：槽 wall time <50ms、终态
   _hotkey_handle 更新为新句柄、old_handle 被 remove_hotkey 移除
2. 连续 3 次失败：终态保留旧热键、retrying 标志复位
3. 重试链在途时再次进入健康检查：被互斥标志拦截，不产生额外尝试
4. 首次即成功：同步完成，不派发任何挂起拍
"""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtWidgets import QApplication

import app.tray_manager as tray_mod
from app.tray_manager import TrayManager


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class FakeKeyboard:
    """add_hotkey 前 fail_times 次抛异常，之后返回新句柄；记录 remove_hotkey。"""

    def __init__(self, fail_times=0):
        self.calls = 0
        self.fail_times = fail_times
        self.removed = []

    def add_hotkey(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(f"hotkey occupied (call {self.calls})")
        return f"new_handle_{self.calls}"

    def remove_hotkey(self, handle):
        self.removed.append(handle)


def _make_manager():
    """最小状态集的 TrayManager（跳过 __init__，只喂 _health_check_hotkey 依赖）。"""
    tm = TrayManager.__new__(TrayManager)  # QObject 子类：__new__ 安全，__init__ 依赖重
    tm._read_hotkey_from_command = lambda: "alt+x"
    tm._ensure_listener_alive = lambda: None

    class _Bridge:
        class toggle_all_windows:  # noqa: N801 - 仅为 add_hotkey 的可传参数
            @staticmethod
            def emit():
                pass

    tm._hotkey_bridge = _Bridge()
    tm._hotkey_handle = "old_handle"
    tm._registered_hotkey = "alt+z"
    tm._hotkey_retrying = False
    return tm


def _pump_until_idle(app, tm, timeout_s=5.0):
    """驱动事件循环直到重试链结束（_hotkey_retrying 复位）。"""
    deadline = time.perf_counter() + timeout_s
    while tm._hotkey_retrying and time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return not tm._hotkey_retrying


def test_retry_succeeds_on_third_attempt(monkeypatch, qapp):
    """连续失败 2 次第 3 次成功：槽不阻塞、终态换新句柄、旧句柄被移除"""
    fake = FakeKeyboard(fail_times=2)
    monkeypatch.setattr(tray_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(tray_mod, "keyboard", fake)
    tm = _make_manager()

    t0 = time.perf_counter()
    tm._health_check_hotkey()
    slot_ms = (time.perf_counter() - t0) * 1000

    assert slot_ms < 50, f"槽函数同步耗时 {slot_ms:.1f}ms，异步化未生效"
    assert fake.calls == 1, "首拍后应挂起等待，不连续同步重试"
    assert tm._hotkey_retrying is True

    assert _pump_until_idle(qapp, tm), "重试链未在超时内收敛"

    assert fake.calls == 3
    assert tm._hotkey_handle == "new_handle_3"
    assert tm._registered_hotkey == "alt+x"
    assert fake.removed == ["old_handle"]
    assert tm._hotkey_retrying is False


def test_all_failures_keep_old_hotkey(monkeypatch, qapp):
    """3 次全失败：保留旧热键，retrying 复位，不误删旧句柄"""
    fake = FakeKeyboard(fail_times=99)
    monkeypatch.setattr(tray_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(tray_mod, "keyboard", fake)
    tm = _make_manager()

    tm._health_check_hotkey()
    assert _pump_until_idle(qapp, tm)

    assert fake.calls == 3
    assert tm._hotkey_handle == "old_handle"
    assert tm._registered_hotkey == "alt+z"
    assert fake.removed == []
    assert tm._hotkey_retrying is False


def test_reentry_guard_during_inflight_retry(monkeypatch, qapp):
    """重试链在途时再次进入：互斥拦截，不派发额外尝试"""
    fake = FakeKeyboard(fail_times=1)
    monkeypatch.setattr(tray_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(tray_mod, "keyboard", fake)
    tm = _make_manager()

    tm._health_check_hotkey()
    assert tm._hotkey_retrying is True

    tm._health_check_hotkey()  # 模拟 5min 周期 timer 重入
    assert fake.calls == 1, "重入应被 _hotkey_retrying 拦截"

    assert _pump_until_idle(qapp, tm)
    assert fake.calls == 2
    assert tm._hotkey_handle == "new_handle_2"


def test_first_attempt_success_is_synchronous(monkeypatch, qapp):
    """首次即成功：同步终态，无挂起拍，旧句柄立即移除"""
    fake = FakeKeyboard(fail_times=0)
    monkeypatch.setattr(tray_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(tray_mod, "keyboard", fake)
    tm = _make_manager()

    t0 = time.perf_counter()
    tm._health_check_hotkey()
    slot_ms = (time.perf_counter() - t0) * 1000

    assert slot_ms < 50
    assert fake.calls == 1
    assert tm._hotkey_retrying is False
    assert tm._hotkey_handle == "new_handle_1"
    assert fake.removed == ["old_handle"]
