# -*- coding: utf-8 -*-
"""[B4] _kill_renderer 异步收尸验证（#18 回滚后：默认同步，显式 DRIFOX_KILL_ASYNC=1 才异步）。

覆盖（#13 规格 a-g + R1 日志观测）：
(a) mock wait 阻塞 1s → _kill_renderer 返回耗时 < 16ms（主线程零阻塞，仅显式开异步）
(b) wait 执行线程非主线程
(c) 返回值语义等价（terminate 成功即 True；拒绝/异常 False 不变）
(d) create_time 不匹配 → 后台不调 wait/kill
(e) --type=renderer 缺失 → False 且不起线程
(f) 默认（不设 env）与显式 DRIFOX_KILL_ASYNC=0 均走同步旧分支
(g) _reap_renderer_async 的 args 不含 self/窗口对象
(h) kill 阶段异常时 debug 日志留痕（R1）
"""
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from app.main_widget import OpenAIChatToolWindow, _reap_renderer_async

GOOD_CMD = '"C:/x/QtWebEngineProcess.exe" --type=renderer --x'
GOOD_NAME = "QtWebEngineProcess.exe"


class _FakeProc:
    """psutil.Process 替身：记录调用，wait 可配置阻塞与超时。"""

    def __init__(self, *, name=GOOD_NAME, cmd=GOOD_CMD, create_time=1000.0,
                 wait_block=0.0, wait_timeout=False):
        self.name_val = name
        self.cmd_val = cmd
        self.create_time_val = create_time
        self.wait_block = wait_block
        self.wait_timeout = wait_timeout
        self.terminated = False
        self.killed = False
        self.wait_calls = []
        self.wait_thread_name = None

    def name(self):
        return self.name_val

    def cmdline(self):
        return self.cmd_val.split()

    def create_time(self):
        return self.create_time_val

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        self.wait_thread_name = threading.current_thread().name
        if self.wait_block:
            time.sleep(self.wait_block)
        if self.wait_timeout:
            import psutil
            raise psutil.TimeoutExpired(self.wait_calls)


@pytest.fixture
def async_on(monkeypatch):
    """显式开启异步收尸（默认已改回同步，见 #18 回滚）"""
    monkeypatch.setenv("DRIFOX_KILL_ASYNC", "1")


def test_a_kill_returns_fast_while_wait_blocks(monkeypatch, async_on):
    """(a) wait 阻塞 1s 时 _kill_renderer 耗时 < 16ms（主线程零阻塞；仅显式开异步时）"""
    fake = _FakeProc(wait_block=1.0)
    monkeypatch.setattr("psutil.Process", lambda pid: fake)
    t0 = time.perf_counter()
    ret = OpenAIChatToolWindow._kill_renderer(123)
    dt_ms = (time.perf_counter() - t0) * 1000
    assert ret is True
    assert dt_ms < 16, f"主线程耗时 {dt_ms:.1f}ms，异步化失效"
    fake.wait_block = 0.0  # 不阻塞解释器退出
    time.sleep(0.05)  # 等后台线程收尾
    assert fake.wait_calls, "后台 reap 未执行 wait"


def test_b_wait_runs_off_main_thread(monkeypatch, async_on):
    """(b) wait 执行线程非主线程（renderer-reap 守卫）"""
    fake = _FakeProc()
    monkeypatch.setattr("psutil.Process", lambda pid: fake)
    assert OpenAIChatToolWindow._kill_renderer(123) is True
    deadline = time.monotonic() + 2.0
    while not fake.wait_calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fake.wait_thread_name is not None
    assert fake.wait_thread_name != threading.current_thread().name
    assert fake.wait_thread_name == "renderer-reap"


def test_c_return_semantics_unchanged(monkeypatch, async_on):
    """(c) 返回值语义：终止发起成功=True；拒绝非 renderer/异常=False（与改前等价）"""
    # 非 renderer（gpu-process）→ False
    fake = _FakeProc(cmd='"C:/x/QtWebEngineProcess.exe" --type=gpu-process')
    monkeypatch.setattr("psutil.Process", lambda pid: fake)
    assert OpenAIChatToolWindow._kill_renderer(1) is False
    assert not fake.terminated
    # 进程名不符 → False
    fake = _FakeProc(name="chrome.exe")
    monkeypatch.setattr("psutil.Process", lambda pid: fake)
    assert OpenAIChatToolWindow._kill_renderer(2) is False
    # NoSuchProcess → False
    import psutil

    def _raise(pid):
        raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr("psutil.Process", _raise)
    assert OpenAIChatToolWindow._kill_renderer(3) is False
    # pid<=0 → False
    assert OpenAIChatToolWindow._kill_renderer(0) is False
    assert OpenAIChatToolWindow._kill_renderer(-5) is False


def test_d_reap_aborts_on_pid_reuse(monkeypatch):
    """(d) create_time 不匹配（pid 复用）→ 后台不调 wait/kill（与开关无关）"""
    called = []
    fake = _FakeProc(create_time=9999.0, wait_block=0.0)  # 与主线程采集的 1000.0 不符
    fake.wait = lambda timeout=None: called.append("wait")
    fake.kill = lambda: called.append("kill")
    _reap_renderer_async(123, 1000.0, GOOD_CMD)
    assert called == [], "pid 复用时后台仍执行了 wait/kill"


def test_e_missing_renderer_flag_no_thread(monkeypatch, async_on):
    """(e) --type=renderer 缺失 → False 且不起线程（无 renderer-reap 线程产生）"""
    before = set(t.name for t in threading.enumerate())
    fake = _FakeProc(cmd='"C:/x/QtWebEngineProcess.exe" --type=utility')
    monkeypatch.setattr("psutil.Process", lambda pid: fake)
    assert OpenAIChatToolWindow._kill_renderer(9) is False
    assert not fake.terminated
    after = set(t.name for t in threading.enumerate())
    assert "renderer-reap" not in (after - before)


def test_f_sync_fallback_and_default_sync(monkeypatch):
    """(f) 同步旧分支：显式 0 与默认（不设 env）都走主线程 wait（#18 回滚后默认同步）"""
    fake = _FakeProc()
    monkeypatch.setattr("psutil.Process", lambda pid: fake)
    monkeypatch.setenv("DRIFOX_KILL_ASYNC", "0")
    assert OpenAIChatToolWindow._kill_renderer(77) is True
    assert fake.wait_calls == [1.0]  # 同分支：wait 被主线程直接调用
    assert fake.wait_thread_name == threading.current_thread().name
    # 默认值（不设 env）：走同步
    fake2 = _FakeProc()
    monkeypatch.setattr("psutil.Process", lambda pid: fake2)
    monkeypatch.delenv("DRIFOX_KILL_ASYNC", raising=False)
    assert OpenAIChatToolWindow._kill_renderer(78) is True
    assert fake2.wait_calls == [1.0]
    assert fake2.wait_thread_name == threading.current_thread().name


def test_g_reap_args_are_pure_values(monkeypatch, async_on):
    """(g) _reap_renderer_async 只收纯值：args 不含 self/窗口/Qt 对象"""
    captured = {}

    orig_thread = threading.Thread

    class _SpyThread(orig_thread):
        def __init__(self, *a, **kw):
            captured["args"] = kw.get("args")
            captured["target"] = kw.get("target")
            super().__init__(*a, **kw)

    monkeypatch.setattr(threading, "Thread", _SpyThread)
    fake = _FakeProc()
    monkeypatch.setattr("psutil.Process", lambda pid: fake)
    assert OpenAIChatToolWindow._kill_renderer(5) is True
    args = captured["args"]
    assert args is not None and len(args) == 3
    pid, create_time, cmd = args
    assert isinstance(pid, int) and isinstance(create_time, float) and isinstance(cmd, str)
    for v in args:
        assert not isinstance(v, OpenAIChatToolWindow)
    assert captured["target"] is _reap_renderer_async


def test_reap_failure_logged(monkeypatch, async_on):
    """(h)[R1] kill 阶段异常：日志留痕（含 pid 与阶段）、函数不抛、正常结束"""
    import psutil
    from app import main_widget as mw

    fake = _FakeProc(wait_timeout=True)
    fake.wait = lambda timeout=None: (_ for _ in ()).throw(psutil.TimeoutExpired(1.0))
    fake.kill = lambda: (_ for _ in ()).throw(RuntimeError("kill boom"))
    monkeypatch.setattr("psutil.Process", lambda pid: fake)
    debug_calls = []
    monkeypatch.setattr(mw.logger, "debug", lambda msg, *a, **k: debug_calls.append(str(msg)))
    _reap_renderer_async(321, fake.create_time_val, GOOD_CMD)  # 不应抛
    assert any("321" in m and "kill" in m for m in debug_calls), (
        f"kill 失败未留日志: {debug_calls}"
    )
