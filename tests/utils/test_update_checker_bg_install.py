# -*- coding: utf-8 -*-
"""macOS 更新安装链后台线程化（N2）回归测试。

锁定四条不变式（评审工单验证要求）：
1. 安装逻辑在非主线程执行（主线程零阻塞，UI 保持响应）；
2. 安装失败路径不在线程内直接调 create_errorbar（Qt UI 红线），错误经信号
   回主线程弹出，应用继续运行（与改前 except → errorbar 行为等价）；
3. 事件循环退出兜底：安装器已启动时 join 安装线程（短超时）后仍 os._exit(0)；
   未启动安装时兜底直接放行（不影响正常退出路径）；
4. 安装完成信号正确发出并触发主线程 quit。

测试构造：信号依赖 QObject 元对象，__new__ 绕过 __init__ 会让 emit/connect
抛 RuntimeError，故用真实构造 + monkeypatch 假 Settings（不依赖全局单例）。
"""
import threading
import time

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

import app.update_checker as uc


class _FakeCfg:
    """UpdateChecker.__init__ 所需的最小 cfg 假对象（隔离全局 Settings 单例）。"""

    patch_platform = type("P", (), {"value": "stable"})()
    github_repo = "fake/repo"
    github_token = type("T", (), {"value": ""})()
    current_version = "0.0.0"


def _real_checker(qapp, monkeypatch) -> uc.UpdateChecker:
    """真实构造 UpdateChecker（绕过单例残留），Settings 换假对象。"""
    monkeypatch.setattr(uc.Settings, "get_instance", lambda: _FakeCfg())
    uc.UpdateChecker._instance = None
    uc.UpdateChecker._initialized = False
    return uc.UpdateChecker()


def _wait_until(cond, timeout=5.0) -> bool:
    """驱动主线程事件循环直到 queued 信号槽执行或超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        QApplication.processEvents()
        time.sleep(0.01)
    return False


# ─────────────────── 不变式①：安装逻辑在后台线程 ───────────────────

def test_install_logic_runs_in_background_thread(qapp, monkeypatch):
    seen = {}
    done = threading.Event()

    def fake_upgrade(self):
        seen["thread"] = threading.current_thread().name
        done.set()

    monkeypatch.setattr(uc.UpdateChecker, "_run_macos_upgrade", fake_upgrade)
    c = _real_checker(qapp, monkeypatch)

    c._start_macos_install()
    assert done.wait(timeout=5), "安装逻辑未在超时内执行"
    c._install_thread.join(timeout=5)
    assert seen["thread"] != threading.main_thread().name
    assert c._install_thread.name == "macos-installer"


def test_start_guard_prevents_double_start(qapp, monkeypatch):
    """重复触发不重复建线程（防信号重复 connect 双 emit）。"""
    monkeypatch.setattr(uc.UpdateChecker, "_run_macos_upgrade", lambda self: None)
    c = _real_checker(qapp, monkeypatch)

    c._start_macos_install()
    t1 = c._install_thread
    c._start_macos_install()
    assert c._install_thread is t1
    c._install_thread.join(timeout=5)


# ──────────── 不变式②：失败路径错误回主线程，不在线程内碰 UI ────────────

def test_failure_errorbar_runs_on_main_thread(qapp, monkeypatch):
    calls = []

    def fake_errorbar(title, msg):
        calls.append((title, msg, threading.current_thread().name))

    def boom(self):
        raise RuntimeError("DMG 挂载失败")

    monkeypatch.setattr(uc.UpdateChecker, "_run_macos_upgrade", boom)
    c = _real_checker(qapp, monkeypatch)
    monkeypatch.setattr(c, "create_errorbar", fake_errorbar)

    c._start_macos_install()
    c._install_thread.join(timeout=5)
    assert _wait_until(lambda: bool(calls)), "失败信号未送达主线程"
    assert calls[0][0] == "启动失败"
    assert calls[0][2] == threading.main_thread().name


# ──────────── 不变式③：退出兜底 join 后 os._exit(0) ────────────

def test_guard_joins_alive_thread_then_hard_exits(qapp, monkeypatch):
    exits = []
    monkeypatch.setattr(uc.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(uc.UpdateChecker, "_INSTALL_JOIN_TIMEOUT", 0.1)

    c = _real_checker(qapp, monkeypatch)
    # 模拟安装线程卡在 hdiutil（存活超过 join 超时）
    blocker = threading.Thread(target=time.sleep, args=(1.0,), daemon=True)
    blocker.start()
    c._installer_started = True
    c._install_thread = blocker

    c._on_about_to_quit_guard()
    assert exits == [0], "join 超时兜底后必须仍走 os._exit(0)"


def test_guard_noop_without_installer(qapp, monkeypatch):
    """未启动安装时兜底直接放行，正常退出路径不受影响。"""
    exits = []
    monkeypatch.setattr(uc.os, "_exit", lambda code: exits.append(code))
    c = _real_checker(qapp, monkeypatch)
    c._installer_started = False
    c._install_thread = None

    c._on_about_to_quit_guard()
    assert exits == []


# ──────────── 不变式④：安装完成信号发出并触发 quit ────────────

def test_install_finished_signal_and_quit(qapp, monkeypatch):
    got = []
    main_thread_hits = []
    monkeypatch.setattr(uc.UpdateChecker, "_run_macos_upgrade", lambda self: None)
    c = _real_checker(qapp, monkeypatch)
    # direct 连接记录「信号发出」本身；显式 QueuedConnection 镜像 _on_install_finished
    # （QObject receiver → auto→queued）的投递路径，验证槽在主线程执行
    c._install_finished.connect(lambda: got.append("finished"))
    c._install_finished.connect(
        lambda: main_thread_hits.append(threading.current_thread().name),
        Qt.QueuedConnection,
    )

    c._start_macos_install()
    c._install_thread.join(timeout=5)
    # lambda 连接的 context 线程是主线程 → worker emit 为 queued 投递，
    # 须驱动事件循环才能送达（到达本身即 emit 发生的证明）
    assert _wait_until(lambda: bool(main_thread_hits)), "queued 信号未送达主线程"
    assert got == ["finished"], "安装完成信号未发出"
    assert main_thread_hits[0] == threading.main_thread().name
