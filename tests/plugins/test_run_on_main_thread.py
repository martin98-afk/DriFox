# -*- coding: utf-8 -*-
"""回归：跨线程主线程投递原语 run_on_main_thread

复现背景（2026-10-29）：市场自动更新跑在纯 ``threading.Thread`` 里，installer
用 ``QTimer.singleShot(0, reload)`` 投递插件重载。纯 Python 线程没有 Qt 事件
循环 → singleShot 既不抛异常也不触发，投递静默丢失 → 插件目录已被替换且
sys.modules 已 purge，旧 UI 注册仍指向旧模块 → 点页脚按钮报
``No module named 'ui_plugin_voice_reply'``。

修复后 installer 改走本原语（QObject 信号桥 post_to_main），跨线程 emit 由
Qt 排队到主线程事件循环，不依赖调用线程有无循环。

锁定点：
1. 主线程调用 → 直接执行（不排队，避免主线程自投递死等）
2. 工作线程调用 wait=True → 阻塞至主线程执行完并拿返回值
3. 工作线程回调抛异常 → 不崩进程，异常回抛给调用线程
4. 工作线程调用 wait=False → 立即返回 True，回调随后在主线程执行
5. 投递超时 → 返回 False（调用方降级），不无限等待
"""

import threading
import time

import pytest

from app.core.services.plugin_host_service import run_on_main_thread


def _serve_main(app, done: threading.Event, seconds: float = 3.0):
    """在主线程泵 Qt 事件循环，直到 done 置位或超时（测试辅助）"""
    deadline = time.time() + seconds
    while time.time() < deadline and not done.is_set():
        app.processEvents()
        time.sleep(0.005)


def test_main_thread_calls_directly(qapp):
    """主线程调用 → 立即同步执行，返回值透过 box 不可见但副作用生效"""
    ran = []
    assert run_on_main_thread(lambda: ran.append(threading.current_thread())) is True
    assert ran == [threading.main_thread()]


def test_worker_thread_waits_and_gets_result(qapp):
    """工作线程 wait=True → 回调在主线程执行，调用线程被阻塞到完成"""
    from app.utils.info_bar_guard import install_guard

    install_guard()  # 预热投递桥（等价于 main.py 的启动序列）
    got = {}
    order = []
    done = threading.Event()

    def _worker():
        order.append("before")
        run_on_main_thread(
            lambda: got.setdefault("thread", threading.current_thread()), wait=True, timeout=3.0
        )
        order.append("after")
        done.set()

    t = threading.Thread(target=_worker, name="plain-worker")
    t.start()
    _serve_main(qapp, done)
    t.join(3)

    assert not t.is_alive(), "工作线程应在主线程执行完回调后返回"
    assert got.get("thread") is threading.main_thread(), "回调必须在主线程执行"
    assert order == ["before", "after"]


def test_callback_exception_does_not_kill_and_reraises(qapp):
    """回调抛异常：不炸主线程，异常回抛给调用线程"""
    from app.utils.info_bar_guard import install_guard

    install_guard()
    box = {}
    done = threading.Event()

    def _worker():
        def _boom():
            raise RuntimeError("boom-in-main")

        try:
            run_on_main_thread(_boom, wait=True, timeout=3.0)
            box["result"] = "no-raise"
        except RuntimeError as e:
            box["result"] = str(e)
        done.set()

    t = threading.Thread(target=_worker, name="plain-worker-err")
    t.start()
    _serve_main(qapp, done)
    t.join(3)

    assert box.get("result") == "boom-in-main", f"实际: {box}"


def test_fire_and_forget_returns_immediately(qapp):
    """wait=False → 立即返回 True，回调随后在主线程执行"""
    from app.utils.info_bar_guard import install_guard

    install_guard()
    ran = []
    t0 = time.time()
    assert run_on_main_thread(lambda: ran.append(threading.current_thread()), wait=False) is True
    assert time.time() - t0 < 1.0, "fire-and-forget 不得阻塞调用线程"
    _serve_main(qapp, threading.Event(), seconds=0.5)
    assert ran == [threading.main_thread()]


def test_timeout_returns_false(qapp):
    """主线程不泵事件 → 等待超时返回 False（调用方降级），不无限挂住"""
    from app.utils.info_bar_guard import install_guard

    install_guard()
    box = {}

    def _worker():
        box["ok"] = run_on_main_thread(lambda: None, wait=True, timeout=0.05)

    t = threading.Thread(target=_worker, name="plain-worker-timeout")
    t.start()
    t.join(3)  # 完全不 processEvents

    assert box.get("ok") is False, f"超时应返回 False，实际: {box}"


def test_no_qapp_calls_directly(monkeypatch):
    """无 QApplication（headless 测试/纯逻辑路径）→ 直接执行并返回 True"""
    from PyQt5.QtWidgets import QApplication

    monkeypatch.setattr(QApplication, "instance", staticmethod(lambda: None))
    ran = []
    assert run_on_main_thread(lambda: ran.append(1)) is True
    assert ran == [1]
