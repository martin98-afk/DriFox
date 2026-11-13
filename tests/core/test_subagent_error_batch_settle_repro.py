# -*- coding: utf-8 -*-
"""回归：子智能体 error/stall 路径绕过完成链，结果永不交付

背景 bug（用户报告）：子智能体「偶尔无法执行，执行完、报错都没有注入消息」。

根因：SubAgentExecutor.run() 有三条出口——
- finished_with_result.emit → manager._on_executor_finished（完成链）
- error_occurred.emit       → 无人监听（subagent_para 传 on_error=None）
- agent not found 裸 return  → 连信号都不发

只要批次内任一任务走 error/stall/cancel 出口，完成链就永远不结算：
UI 面板行收不了尾（task_finished 不发），逐条结果注入也不发生，
对话里永远等不到通知。

补充（2026-11-13）：批次汇总「子智能体全部完成。共 N 个…」已停发，
逐条注入是唯一交付出口，出错任务的错误正文必须随单条注入出去。

本测试不启动真实 executor/LLM，只驱动 manager 的完成链。
"""

import importlib.util
from pathlib import Path

_MANAGER_PATH = Path(__file__).resolve().parents[2] / "plugins" / "system-subagent" / "manager.py"
_spec = importlib.util.spec_from_file_location("test_error_settle_manager", _MANAGER_PATH)
assert _spec is not None and _spec.loader is not None
_manager_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_manager_mod)
SubAgentManager = _manager_mod.SubAgentManager

_RESULT_OK = "任务二的正常结果"
_ERR_MSG = "SubAgent execution error: API timeout"


class _IdleBackend:
    """主对话已停在本轮等待注入（非流式窗口，最常见的派发场景）"""

    class _Engine:
        is_streaming = False

    chat_engine = _Engine()
    _hook_message_queue = None


class _ToolExecutor:
    def __init__(self, backend):
        self._backend = backend


class _FakeExecutor:
    def __init__(self, task_id, session_id="sess-1", error=None):
        self.agent_name = "explore"
        self.task_description = f"任务 {task_id}"
        self._execution_error = error
        self._task_session_id = session_id
        self._last_result = ""
        self.last_result = ""
        self.execution_error = error or ""
        self.tool_call_count = 1
        self.start_time = 0

    def isFinished(self):
        return True

    def isRunning(self):
        return False

    def get_logs(self):
        return []

    def get_summary(self):
        return {"task_id": "t", "agent_name": "explore"}


def _make_manager():
    mgr = SubAgentManager(
        agent_manager=None,
        tool_executor=_ToolExecutor(_IdleBackend()),
        get_llm_config=lambda: {},
    )
    injected = []
    mgr._resolve_result_injector = lambda: (
        lambda send, task_id, result, success, agent_name, task_description, mode="hook": injected.append(
            (task_id, result, success)
        )
        or True
    )
    mgr.set_session_send(lambda text, **kwargs: True)
    mgr._test_injected = injected
    return mgr


def _seed_batch(mgr, task_ids):
    """派发一个 N 任务批次：批次计数 + 运行中条目"""
    mgr._batch_total = len(task_ids)
    mgr._batch_completed = 0
    mgr._batch_task_ids = set(task_ids)
    for tid in task_ids:
        mgr._running_tasks[tid] = _FakeExecutor(tid)
    return mgr


def test_error_task_settled_and_injected():
    """t1 走 error 出口：必须进完成链结算并逐条注入（带失败标记）。"""
    mgr = _make_manager()
    _seed_batch(mgr, ["t1", "t2"])
    batch_messages = []
    mgr.batch_finished.connect(lambda content, failed: batch_messages.append((content, failed)))
    finished = []
    mgr.task_finished.connect(lambda tid, result: finished.append(tid))

    mgr._on_executor_error("t1", _ERR_MSG)
    mgr._on_executor_finished("t2", _RESULT_OK)

    assert [item[0] for item in mgr._test_injected] == ["t1", "t2"]
    assert mgr._test_injected[0][2] is False, "error 任务注入必须带失败标记"
    assert mgr._test_injected[0][1] == _ERR_MSG, "错误正文要作为注入正文，否则模型只看到 ❌"
    assert "t1" in finished and "t2" in finished, "面板收尾依赖 task_finished"
    assert batch_messages == [], "批次汇总已停发"


def test_error_then_late_finished_signal_is_idempotent():
    """同一线程先发 error_occurred、后发 finished_with_result（cancel 后退出）：
    第二次信号必须被 _settled_tasks 拦住，注入不超发。"""
    mgr = _make_manager()
    _seed_batch(mgr, ["t1", "t2"])

    mgr._on_executor_error("t1", _ERR_MSG)
    mgr._on_executor_finished("t1", "迟到的完成信号")
    mgr._on_executor_finished("t2", _RESULT_OK)

    assert [item[0] for item in mgr._test_injected] == ["t1", "t2"], "同一任务只注入一次"
    assert all(item[1] != "迟到的完成信号" for item in mgr._test_injected)


def test_stall_task_settled():
    """t1 被 stall 检测器取消：stall 路径手工归档后统一结算，仍要注入。"""
    mgr = _make_manager()
    _seed_batch(mgr, ["t1", "t2"])
    mgr._finished_tasks["t1"] = {
        "result": "",
        "error": "Task stalled: no log activity for 300s (timeout=300s)",
        "agent_name": "explore",
        "task_description": "任务 t1",
        "session_id": "sess-1",
    }
    del mgr._running_tasks["t1"]

    mgr._on_executor_finished("t1", "")
    mgr._on_executor_finished("t2", _RESULT_OK)

    assert [item[0] for item in mgr._test_injected] == ["t1", "t2"]
    assert mgr._test_injected[0][2] is False


def test_all_error_batch_still_injects_each():
    """整批全灭（如代理配置错误导致全部秒挂）：每条错误都要逐条注入。"""
    mgr = _make_manager()
    _seed_batch(mgr, ["t1", "t2"])
    for tid in ("t1", "t2"):
        mgr._running_tasks[tid]._execution_error = _ERR_MSG

    mgr._on_executor_finished("t1", "")
    mgr._on_executor_finished("t2", "")

    assert [item[0] for item in mgr._test_injected] == ["t1", "t2"]
    assert all(item[2] is False for item in mgr._test_injected)
