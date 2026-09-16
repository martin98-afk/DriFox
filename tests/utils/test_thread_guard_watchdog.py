# -*- coding: utf-8 -*-
"""thread_guard 看门狗骨架测试（#2.8 清单 3）"""

from app.utils.thread_guard import (
    _STUCK_TIMEOUT_S,
    _WATCHDOG_INTERVAL_S,
    _scan_stuck_threads,
    _watchdog_thread,
    install_guard,
    start_watchdog,
)


def test_constants_have_safe_defaults():
    """看门狗阈值与间隔常量取保守默认值（60s/30s）。"""
    assert _STUCK_TIMEOUT_S == 60
    assert _WATCHDOG_INTERVAL_S == 30


def test_watchdog_starts_idempotently():
    """start_watchdog 可重复调用且守护线程存活。"""
    install_guard()
    start_watchdog()
    start_watchdog()  # 第二次幂等
    assert _watchdog_thread is not None
    assert _watchdog_thread.is_alive()
    assert _watchdog_thread.daemon is True
    assert _watchdog_thread.name == "ThreadGuardWatchdog"


def test_watchdog_tolerates_deleted_cpp_thread(qapp):
    """C++ 侧已析构的 QThread 不得让看门狗抛 RuntimeError，且条目要被剔除。

    回归现场：
        Exception in thread ThreadGuardWatchdog:
        RuntimeError: wrapped C/C++ object of type QThread has been deleted
    """
    from PyQt5 import sip
    from PyQt5.QtCore import QThread

    from app.utils import thread_guard as tg

    tg.install_guard()
    t = QThread()
    assert t in tg._running_threads, "install_guard 应已把新线程登记进墓地"
    # 模拟 Qt 主线程销毁 C++ 对象、但集合里条目未被 destroyed 回调清掉
    sip.delete(t)
    assert sip.isdeleted(t)
    try:
        tg._scan_stuck_threads()  # 修复前：此处抛 RuntimeError 打断整个看门狗线程
    finally:
        try:
            tg._running_threads.discard(t)
        except RuntimeError:
            pass
    # 再扫一轮：确认失效条目已被剔除，不会每 30s 重复抛
    tg._scan_stuck_threads()


# ========== T15：退出期 atexit 清理 ==========

from PyQt5.QtCore import QThread


def _make_fake_running_thread():
    """spec=QThread 的 Mock（通过 isinstance 守卫），标记为运行中。"""
    from unittest.mock import MagicMock

    t = MagicMock(spec=QThread)
    t.isRunning.return_value = True
    return t


def test_atexit_cleanup_requests_interruption_quit_wait(qapp, monkeypatch):
    """atexit 清理：存活线程按序收到 requestInterruption/quit/wait(500)。"""
    from app.utils import thread_guard as tg

    tg.install_guard()
    t = _make_fake_running_thread()
    monkeypatch.setattr(tg, "_cpp_alive", lambda obj: True)
    tg._running_threads.add(t)
    try:
        tg._cleanup_threads_at_exit()
        t.requestInterruption.assert_called_once()
        t.quit.assert_called_once()
        t.wait.assert_called_once_with(tg._atexit_wait_ms)
        assert t.wait.call_args[0][0] <= 500, "单线程等待上限 500ms"
        assert tg._atexit_registered is True, "install_guard 应已注册 atexit"
    finally:
        tg._running_threads.discard(t)


def test_atexit_cleanup_order_interrupt_first(qapp, monkeypatch):
    """清理顺序必须 interrupt → quit → wait：先给业务层中断检查点机会。"""
    from app.utils import thread_guard as tg

    t = _make_fake_running_thread()
    monkeypatch.setattr(tg, "_cpp_alive", lambda obj: True)
    order = []
    t.requestInterruption.side_effect = lambda: order.append("interrupt")
    t.quit.side_effect = lambda: order.append("quit")
    t.wait.side_effect = lambda *_: order.append("wait")
    tg._running_threads.add(t)
    try:
        tg._cleanup_threads_at_exit()
        assert order == ["interrupt", "quit", "wait"]
    finally:
        tg._running_threads.discard(t)


def test_atexit_cleanup_skips_dead_cpp_thread(qapp, monkeypatch):
    """C++ 侧已析构的条目被跳过：不得触碰线程方法（触了就 RuntimeError）。"""
    from app.utils import thread_guard as tg

    t = _make_fake_running_thread()
    monkeypatch.setattr(tg, "_cpp_alive", lambda obj: False)
    tg._running_threads.add(t)
    try:
        tg._cleanup_threads_at_exit()
        t.requestInterruption.assert_not_called()
        t.quit.assert_not_called()
        t.wait.assert_not_called()
    finally:
        tg._running_threads.discard(t)


def test_atexit_cleanup_never_terminates_deletes(qapp, monkeypatch):
    """清理绝不 terminate/deleteLater/setParent：这些是退出期另一条 qFatal 链。"""
    from app.utils import thread_guard as tg

    t = _make_fake_running_thread()
    monkeypatch.setattr(tg, "_cpp_alive", lambda obj: True)
    tg._running_threads.add(t)
    try:
        tg._cleanup_threads_at_exit()
        t.terminate.assert_not_called()
        t.deleteLater.assert_not_called()
        t.setParent.assert_not_called()
    finally:
        tg._running_threads.discard(t)


def test_run_overridden_thread_interruptible(qapp):
    """run() 重写型（无事件循环）线程：requestInterruption 后可自然退出。"""
    import time as _t

    class _SpinWorker(QThread):
        """重写 run() 的纯 while 循环线程（无 exec_，quit() 对它无效）。"""

        def __init__(self):
            super().__init__()
            self.steps = 0

        def run(self):
            while not self.isInterruptionRequested():
                self.steps += 1
                _t.sleep(0.01)

    w = _SpinWorker()
    w.start()
    try:
        # wait() 等的是线程结束，等「启动」要用 isRunning 轮询；
        # 再等 steps > 0 确保 run() 已进循环体（否则中断标志抢跑，验证不了循环打断）
        deadline = _t.monotonic() + 2.0
        while w.steps == 0 and _t.monotonic() < deadline:
            _t.sleep(0.01)
        assert w.steps > 0, "线程应已进入 run() 循环体"
        w.requestInterruption()
        assert w.wait(1000), "中断后线程应在 1s 内自然退出（M6 检查点生效）"
        assert w.steps > 0
    finally:
        from app.utils import thread_guard as _tg

        # 兜底：断言失败路径也不能留活线程（否则解释器退出 qFatal，恰为本任务要根除的链）
        w.requestInterruption()
        w.wait(1000)
        _tg._running_threads.discard(w)


def test_atexit_cleanup_budget_warning(caplog, monkeypatch):
    """总时长超预算（默认 1s）时输出 WARNING（监控口径）。"""
    from app.utils import thread_guard as tg

    t = _make_fake_running_thread()
    monkeypatch.setattr(tg, "_cpp_alive", lambda obj: True)
    monkeypatch.setattr(tg, "_atexit_budget_s", -1.0)  # 强制视为超预算
    tg._running_threads.add(t)
    try:
        with caplog.at_level("WARNING", logger="thread_guard.watchdog"):
            tg._cleanup_threads_at_exit()
        assert any("atexit 清理耗时" in r.message for r in caplog.records)
    finally:
        tg._running_threads.discard(t)


# ========== 回归：非 Qt 管理线程里创建 QThread 不得自递归 ==========


def test_cross_thread_qthread_creation_no_self_recursion(qapp, caplog):
    """线程池类线程里 new QThread 不得触发 QThread.__init__ 自递归。

    回归现场（2026-09-16）：
        _safe_init 内调 QThread.currentThread() 判断是否主线程，而 sip 为
        「非 Qt 管理的线程」（adopted thread，即 ThreadPoolExecutor 的
        tool_parallel_*）生成 QThread 包装对象时会再走一次 __init__ →
        无限递归 → C 栈打满：RecursionError: Stack overflow (used 1954 kB)，
        subagent_para 派发必失败，整个子智能体通路不可用。
    """
    import threading as _th

    from app.utils import thread_guard as tg

    tg.install_guard()
    result = {}

    def _work():
        try:
            t = QThread()
            result["ok"] = t is not None
            tg._running_threads.discard(t)  # 不污染其他用例的扫描集合
        except BaseException as e:  # noqa: BLE001
            result["err"] = f"{type(e).__name__}: {e}"

    # 32MB 栈：万一回归，让 Python recursionlimit 先触发并抛 RecursionError，
    # 而不是把 C 栈打满带崩整个 pytest 进程（后者连 traceback 都拿不到）。
    old = _th.stack_size(32 * 1024 * 1024)
    th = _th.Thread(target=_work, name="tool_parallel_probe")
    try:
        with caplog.at_level("WARNING", logger="thread_guard.watchdog"):
            th.start()
            th.join(30)
    finally:
        _th.stack_size(old)

    assert not th.is_alive(), "探测线程超时未返回（疑似栈溢出把线程卡死）"
    assert "err" not in result, f"跨线程创建 QThread 失败（疑似 __init__ 自递归）: {result.get('err')}"
    assert result.get("ok") is True, "跨线程创建 QThread 应成功（守卫只告警，不阻断）"
    assert any("非主线程创建 QThread" in r.message for r in caplog.records), "应留下 affinity 告警"
