# -*- coding: utf-8 -*-
"""子智能体任务状态同步回归测试

背景 bug：任务完成时 DB 状态永挂 running，会话卡片（SubAgentSessionCard）轮询
get_task_logs 命中 DB 即返回，导致卡片永远显示「执行中」、工具数/时间冻结。

两条修复路径的锁定：
- mark_task_finished：完成归档 + 落库 finished（正常回调路径）
- get_task_logs 前置 get_finished_tasks：兜底无回调路径（cancel 等）
"""

import time

import pytest

from app.core.workers.subagent_worker import SubAgentExecutor, SubAgentManager


class _FakeStore:
    """有状态假 SessionStore：模拟 subagent 任务接口，语义对齐真实 repository。

    save_subagent_task = 全量 upsert（summary=None 会清空）；
    update_subagent_task_status = 部分更新（None 字段不覆盖）。
    """

    def __init__(self):
        self._tasks = {}

    def save_subagent_task(
        self,
        task_id,
        agent_name,
        task_description,
        status="running",
        result=None,
        error=None,
        logs=None,
        summary=None,
        session_id="",
    ):
        self._tasks[task_id] = {
            "task_id": task_id,
            "agent_name": agent_name,
            "task_description": task_description,
            "session_id": session_id,
            "status": status,
            "result": result or "",
            "error": error or "",
            "logs": logs or [],
            "summary": summary or {},
        }
        return True

    def update_subagent_task_status(self, task_id, status, result=None, error=None, logs=None, summary=None):
        task = self._tasks.get(task_id)
        if not task:
            return False
        task["status"] = status
        if result is not None:
            task["result"] = result
        if error is not None:
            task["error"] = error
        if logs is not None:
            task["logs"] = logs
        if summary is not None:
            task["summary"] = summary
        return True

    def get_subagent_task(self, task_id):
        task = self._tasks.get(task_id)
        return dict(task) if task else None


def _make_manager(store):
    mgr = SubAgentManager(agent_manager=None, tool_executor=None, get_llm_config=lambda: {})
    mgr.set_session_store(store)
    return mgr


def _make_executor(task_id="t1", tool_calls=3):
    """构造一个模拟已运行过的 executor（不 start 线程）"""
    ex = SubAgentExecutor(
        task_id=task_id,
        agent_name="explore",
        task_description="探查代码",
        llm_config={"模型名称": "test-model"},
        agent_manager=None,
        tool_executor=None,
    )
    ex._start_time = time.time() - 120  # 已跑 2 分钟
    ex._tool_call_count = tool_calls
    ex._last_result = "任务完成结果"
    ex._logs = [
        {"type": "progress", "content": "开始", "timestamp": time.time()},
        {"type": "tool_call", "content": "grep x", "timestamp": time.time()},
    ]
    ex._task_session_id = "sess-1"
    # 模拟"线程已跑完"终态：不 start 线程时 QThread.isFinished() 返回 False，
    # 而 get_finished_tasks 归档的前提就是 isFinished()——实例级覆盖模拟已完成。
    ex.isFinished = lambda: True
    return ex


def _seed_running_record(store, task_id="t1"):
    """模拟 executor 实时日志回调写入的 running 记录（bug 触发前提）"""
    store.save_subagent_task(
        task_id,
        "explore",
        "探查代码",
        "running",
        "",
        "",
        [{"type": "progress", "content": "开始", "timestamp": time.time()}],
        {"task_id": task_id, "tool_call_count": 3, "elapsed_seconds": 100},
        session_id="sess-1",
    )


@pytest.fixture
def store():
    return _FakeStore()


class TestMarkTaskFinished:
    """修复 A：完成归档 + 落库 finished"""

    def test_finished_persisted_to_store(self, store):
        mgr = _make_manager(store)
        _seed_running_record(store)
        ex = _make_executor()
        mgr._running_tasks["t1"] = ex

        info = mgr.mark_task_finished("t1", "任务完成结果", "")

        assert "t1" not in mgr._running_tasks
        assert mgr._finished_tasks["t1"]["result"] == "任务完成结果"
        assert mgr._finished_tasks["t1"]["tool_call_count"] == 3
        assert info["agent_name"] == "explore"
        assert info["session_id"] == "sess-1"

        record = store.get_subagent_task("t1")
        assert record["status"] == "finished"
        # summary 必须带最终数据（不能被全量 upsert 清空）
        assert record["summary"]["tool_call_count"] == 3
        assert record["summary"]["elapsed_seconds"] > 0

    def test_existing_entry_not_overwritten(self, store):
        """DAG 场景：_finished_tasks 已有条目（如跳过信息），error 不被空覆盖；summary 保留运行期值"""
        mgr = _make_manager(store)
        _seed_running_record(store)
        mgr._finished_tasks["t1"] = {
            "result": "",
            "error": "skipped by DAG",
            "agent_name": "explore",
            "task_description": "探查代码",
            "session_id": "sess-1",
            "logs": [{"type": "progress", "content": "跳过", "timestamp": time.time()}],
        }

        mgr.mark_task_finished("t1", "最终结果", "")

        entry = mgr._finished_tasks["t1"]
        assert entry["error"] == "skipped by DAG"  # 空 error 不覆盖已有
        assert entry["result"] == "最终结果"

        record = store.get_subagent_task("t1")
        assert record["status"] == "finished"
        assert record["error"] == "skipped by DAG"
        # summary 未显式提供时不被清空（部分更新语义）
        assert record["summary"]["tool_call_count"] == 3


class TestGetTaskLogsAfterFinish:
    """修复 B：get_task_logs 先归档再查库，无回调路径也能看到 finished"""

    def test_db_shortcircuit_returns_finished(self, store):
        """DB 有 running 记录 + executor 已完成 → 查询后状态必须推进到 finished"""
        mgr = _make_manager(store)
        _seed_running_record(store)
        ex = _make_executor()
        mgr._running_tasks["t1"] = ex

        data = mgr.get_task_logs("t1")

        assert data["found"] is True
        assert data["status"] == "finished"
        assert data["summary"]["tool_call_count"] == 3
        assert "t1" not in mgr._running_tasks
