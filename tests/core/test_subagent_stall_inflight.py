# -*- coding: utf-8 -*-
"""回归：子智能体被 stall 看门狗误杀 + 取消原因被覆写成笼统 Task cancelled

## 背景（用户报告）
「子智能体经常出现执行结果 Task cancelled，实际我并没有取消执行」。
真机日志（C:/Users/mading/.drifox/logs/all.log）：

    [ThreadGuard] 疑似卡死线程 SubAgentExecutor (已运行 74s → 374s)
    [SubAgentManager] ⚠️ Task f8365cf1 (build) stalled for 301s, cancelling
    [SubAgentExecutor] Task f8365cf1... cancelled, emitting error_occurred

两层缺陷：
1. 误杀：stall 用「日志静默 300s」判活，但 `_last_activity_time` 只在日志/
   迭代边界刷新。长思考流式聚合、API 重试等待、长工具执行期间合法静默，
   健康任务跑满 300s 无日志即被当作死锁杀掉。
2. 文案误导：stall 路径写了真因，但 worker 的 cancel 分支随后无条件把
   `_execution_error` 与 DB 归档覆写成 "Task cancelled"，会话卡只剩「❌ 执行失败」。

## 修复语义
- executor 暴露「在途」阶段（API 调用含重试/流式聚合、工具执行），在途期间
  stall 豁免；单次在途超过硬上限仍判真死锁，防线程永久泄漏。
- cancel(reason) 透传真因，worker cancel 分支用真因归档与发信号。
"""
import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

from app.core.workers.subagent_worker import SubAgentExecutor

_MANAGER_PATH = Path(__file__).resolve().parents[2] / "plugins" / "system-subagent" / "manager.py"
_spec = importlib.util.spec_from_file_location("test_stall_inflight_manager", _MANAGER_PATH)
assert _spec is not None and _spec.loader is not None
_manager_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_manager_mod)
SubAgentManager = _manager_mod.SubAgentManager


# ── manager 侧：stall 判活 ──────────────────────────────────────

class _StallExecutor:
    """只实现 _check_stalled_tasks 读到的接口的最小替身。"""

    def __init__(self, task_id, idle_seconds, inflight_seconds=None):
        self.task_id = task_id
        self.agent_name = "build"
        self.task_description = "调查任务"
        self._task_session_id = "sess-1"
        self._idle_seconds = idle_seconds
        self._inflight_seconds = inflight_seconds
        self.cancelled = []
        self._execution_error = None

    def isRunning(self):
        return True

    def get_last_activity_time(self):
        return time.time() - self._idle_seconds

    def get_in_flight_duration(self):
        return self._inflight_seconds

    def cancel(self, reason="Task cancelled"):
        self.cancelled.append(reason)

    def get_logs(self):
        return []

    def get_summary(self):
        return {}


def _make_manager(monkeypatch):
    mgr = SubAgentManager(
        agent_manager=None,
        tool_executor=SimpleNamespace(_backend=None),
        get_llm_config=lambda: {},
    )
    # 只验 cancel 判定，隔离完成链与 DB 写入
    monkeypatch.setattr(mgr, "_on_executor_finished", lambda *a, **kw: None)
    monkeypatch.setattr(mgr, "_save_task_to_store", lambda *a, **kw: None)
    return mgr


def test_inflight_operation_not_killed_by_stall(monkeypatch):
    """核心回归：API/工具在途期间长时间无日志是合法的，不得误杀。"""
    mgr = _make_manager(monkeypatch)
    ex = _StallExecutor("t1", idle_seconds=900, inflight_seconds=120)
    mgr._running_tasks["t1"] = ex

    mgr._check_stalled_tasks()

    assert ex.cancelled == [], "在途阶段必须豁免 stall 判定"
    assert "t1" in mgr._running_tasks


def test_idle_task_still_killed_with_reason(monkeypatch):
    """不在途且静默超标仍按 stall 杀，且 cancel 带真因而非笼统 Task cancelled。"""
    mgr = _make_manager(monkeypatch)
    ex = _StallExecutor("t1", idle_seconds=400, inflight_seconds=None)
    mgr._running_tasks["t1"] = ex

    mgr._check_stalled_tasks()

    assert len(ex.cancelled) == 1
    assert "stalled" in ex.cancelled[0]
    assert "no log activity" in ex.cancelled[0]


def test_inflight_over_hard_limit_still_killed(monkeypatch):
    """在途但超过硬上限 = 真死锁，仍杀且原因写明 in-flight 超时。"""
    mgr = _make_manager(monkeypatch)
    ex = _StallExecutor(
        "t1", idle_seconds=9999, inflight_seconds=mgr.IN_FLIGHT_MAX_SECONDS + 10
    )
    mgr._running_tasks["t1"] = ex

    mgr._check_stalled_tasks()

    assert len(ex.cancelled) == 1
    assert "in-flight" in ex.cancelled[0]


def test_recent_activity_not_killed(monkeypatch):
    """刚有活动（无论是否在途）都不该被杀，防回归。"""
    mgr = _make_manager(monkeypatch)
    ex = _StallExecutor("t1", idle_seconds=10, inflight_seconds=None)
    mgr._running_tasks["t1"] = ex

    mgr._check_stalled_tasks()

    assert ex.cancelled == []


# ── executor 侧：在途阶段与取消原因 ──────────────────────────────

class _FakeAgent:
    inherit_history = False
    inherit_history_count = None

    def is_subagent(self):
        return True


class _FakeAgentManager:
    def get_agent(self, name):
        return _FakeAgent()

    def get_agent_tools_schema(self, *a, **kw):
        return []

    def get_agent_system_prompt(self, *a, **kw):
        return ""


def _build_executor():
    return SubAgentExecutor(
        task_id="t_cancel",
        agent_name="build",
        task_description="任务",
        llm_config={},
        agent_manager=_FakeAgentManager(),
        tool_executor=None,
    )


def test_inflight_phase_tracks_duration_and_touches_activity():
    """在途计数可重入；退出必须刷新活跃时间，避免边界用旧时间戳被误杀。"""
    ex = _build_executor()
    assert ex.get_in_flight_duration() is None

    ex._last_activity_time = 0.0
    with ex._in_flight_phase():
        assert ex.get_in_flight_duration() is not None
        with ex._in_flight_phase():
            assert ex.get_in_flight_duration() is not None
        assert ex.get_in_flight_duration() is not None

    assert ex.get_in_flight_duration() is None
    assert ex._last_activity_time > 0, "退出在途必须刷新活跃时间"


def test_api_and_tool_methods_are_inflight_wrapped():
    """_make_api_call / _execute_tools 必须挂在途包络（functools.wraps 留 __wrapped__）。"""
    assert hasattr(SubAgentExecutor._make_api_call, "__wrapped__"), "API 调用未纳入在途"
    assert hasattr(SubAgentExecutor._execute_tools, "__wrapped__"), "工具执行未纳入在途"


def test_cancel_default_reason_backward_compatible(monkeypatch):
    """不传 reason 的旧调用方（cancel_task 等）终态仍报 Task cancelled。"""
    ex = _build_executor()
    emitted = []
    ex.error_occurred.connect(lambda tid, err: emitted.append(err))

    def _loop(*a, **kw):
        ex.cancel()  # 旧式无参调用
        return ""

    monkeypatch.setattr(ex, "_execute_agent_loop", _loop)
    monkeypatch.setattr(ex, "get_summary", lambda: {})
    ex.run()

    assert ex._cancel_reason == "Task cancelled"
    assert emitted == ["Task cancelled"], "默认取消原因须贯穿到终态信号"


def test_second_cancel_does_not_overwrite_reason():
    """stall 后窗口关闭等二次无参 cancel() 不得把真因冲掉。"""
    ex = _build_executor()
    ex.cancel("Task stalled: no log activity for 301s")
    ex.cancel()
    assert "stalled" in ex._cancel_reason


def test_cancel_reason_reaches_signal_and_archive(monkeypatch):
    """真跑 run() 的 cancel 分支：emit 与归档都用真因，不再覆写为 Task cancelled。"""
    reason = "Task stalled: no log activity for 301s (timeout=300s)"
    ex = _build_executor()

    archive = []
    ex.set_log_store_callback(lambda *args: archive.append(args))
    emitted = []
    ex.error_occurred.connect(lambda tid, err: emitted.append(err))

    def _loop(*a, **kw):
        ex.cancel(reason)  # 模拟看门狗在执行途中置位
        return ""

    monkeypatch.setattr(ex, "_execute_agent_loop", _loop)
    monkeypatch.setattr(ex, "get_summary", lambda: {})
    ex.run()

    assert emitted == [reason], "error_occurred 必须带真因"
    assert archive, "取消终态必须落归档"
    status, result, error = archive[-1][3], archive[-1][4], archive[-1][5]
    assert status == "cancelled"
    assert error == reason, "DB 归档 error 不得被覆写成 Task cancelled"
