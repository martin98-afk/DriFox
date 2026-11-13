# -*- coding: utf-8 -*-
"""回归：子智能体结果逐条交付，不等同批任务全部完成

背景（用户报告）：多个子智能体并行时，先完成的任务结果要等最慢那个完成才
一次性注入。根因是 ``_on_executor_finished`` 里 ``if streaming:`` 才调注入器，
空闲态整批攒到批次末尾（一个 stall 就扣住全部已完成结果，最长 300s）。

修复语义：
1. 空闲态：任务一完成就调注入器（hook 降级为 send 开新轮），不等批次。
2. 流式态：仍逐条静默注入（原行为，回归保护）。
3. 批次汇总（「子智能体全部完成。共 N 个…」）2026-11-13 停发：
   每条结果已逐条注入，末尾汇总只是重复通知，还要为它多开一轮对话。
"""

import importlib.util
from pathlib import Path

_MANAGER_PATH = Path(__file__).resolve().parents[2] / "plugins" / "system-subagent" / "manager.py"
_spec = importlib.util.spec_from_file_location("test_per_task_injection_manager", _MANAGER_PATH)
assert _spec is not None and _spec.loader is not None
_manager_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_manager_mod)
SubAgentManager = _manager_mod.SubAgentManager

_RESULT_T1 = "任务一的结果正文"
_RESULT_T2 = "任务二的结果正文"
_ERR_MSG = "SubAgent execution error: API timeout"


class _Backend:
    """可切换忙闲的宿主后端"""

    def __init__(self, streaming: bool):
        backend = self

        class _Engine:
            is_streaming = streaming

        self.chat_engine = _Engine()
        self._hook_message_queue = None


class _ToolExecutor:
    def __init__(self, backend):
        self._backend = backend


class _FakeExecutor:
    def __init__(self, task_id, error=None):
        self.agent_name = "explore"
        self.task_description = f"任务 {task_id}"
        self._execution_error = error
        self._task_session_id = "sess-1"
        self._last_result = ""


def _make_manager(streaming=False):
    mgr = SubAgentManager(
        agent_manager=None,
        tool_executor=_ToolExecutor(_Backend(streaming)),
        get_llm_config=lambda: {},
    )
    injected = []

    # 注入器替身：记录 (task_id, result, success) 并吞掉 send
    mgr._resolve_result_injector = lambda: (
        lambda send, task_id, result, success, agent_name, task_description, mode="hook": injected.append(
            (task_id, result, success)
        )
        or True
    )
    mgr.set_session_send(lambda text, **kwargs: True)
    return mgr, injected


def _seed_batch(mgr, task_ids):
    mgr._batch_total = len(task_ids)
    mgr._batch_completed = 0
    mgr._batch_task_ids = set(task_ids)
    for tid in task_ids:
        mgr._running_tasks[tid] = _FakeExecutor(tid)
        mgr._finished_tasks[tid] = {
            "result": "",
            "error": "",
            "agent_name": "explore",
            "task_description": f"任务 {tid}",
            "session_id": "sess-1",
        }
    return mgr


def test_idle_task_injects_immediately_without_waiting_batch():
    """核心回归：空闲态 t1 完成即注入，此时 t2 尚未完成。"""
    mgr, injected = _make_manager(streaming=False)
    _seed_batch(mgr, ["t1", "t2"])

    mgr._on_executor_finished("t1", _RESULT_T1)

    assert injected == [("t1", _RESULT_T1, True)], "t1 完成即应注入，不能等 t2"

    mgr._on_executor_finished("t2", _RESULT_T2)

    assert [item[0] for item in injected] == ["t1", "t2"], "每条结果各自注入一次"
    assert injected[1][1] == _RESULT_T2


def test_streaming_still_injects_each_task():
    """流式态逐条静默注入（原行为不回退），全完成不再追加汇总。"""
    mgr, injected = _make_manager(streaming=True)
    _seed_batch(mgr, ["t1", "t2"])

    mgr._on_executor_finished("t1", _RESULT_T1)
    assert [item[0] for item in injected] == ["t1"]

    mgr._on_executor_finished("t2", _RESULT_T2)

    assert [item[0] for item in injected] == ["t1", "t2"]


def test_batch_summary_not_emitted_when_all_done():
    """全部完成不再广播汇总（2026-11-13 停发决策）。"""
    mgr, _injected = _make_manager(streaming=False)
    _seed_batch(mgr, ["t1", "t2"])
    summaries = []
    mgr.batch_finished.connect(lambda content, failed: summaries.append((content, failed)))

    mgr._on_executor_finished("t1", _RESULT_T1)
    mgr._on_executor_finished("t2", _RESULT_T2)

    assert summaries == [], "批次汇总已停发，不得再广播"


def test_batch_counters_reset_after_all_done():
    """汇总停发但批次记账仍要复位，否则下一批任务永远凑不齐。"""
    mgr, _injected = _make_manager(streaming=False)
    _seed_batch(mgr, ["t1", "t2"])

    mgr._on_executor_finished("t1", _RESULT_T1)
    mgr._on_executor_finished("t2", _RESULT_T2)

    assert mgr._batch_total == 0
    assert mgr._batch_completed == 0
    assert mgr._batch_task_ids == set()


def test_failed_task_still_injected_individually():
    """失败任务逐条注入仍带失败标记（汇总停发后这是唯一交付出口）。"""
    mgr, injected = _make_manager(streaming=False)
    _seed_batch(mgr, ["t1", "t2"])
    mgr._running_tasks["t1"]._execution_error = _ERR_MSG
    mgr._finished_tasks["t1"]["error"] = _ERR_MSG
    summaries = []
    mgr.batch_finished.connect(lambda content, failed: summaries.append((content, failed)))

    mgr._on_executor_finished("t1", "")
    mgr._on_executor_finished("t2", _RESULT_T2)

    assert injected[0][0] == "t1" and injected[0][2] is False
    assert injected[1][0] == "t2" and injected[1][2] is True
    assert summaries == []
