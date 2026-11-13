# -*- coding: utf-8 -*-
"""回归：交接切会话后，子智能体结果仍要能被新会话拿到

背景 bug：会话 A 派发后台任务 → update_handoff(switch=true) 切到会话 B →
子任务在 B 的引擎空闲窗口完成。此时单任务 hook 注入被 _is_engine_streaming
门控跳过，批次兜底汇总又只有任务清单、不带结果正文，模型在新会话里看不到
任何执行结果；subagent_status 无参还被会话隔离挡住，查不到旧任务。

两条修复：
- A：单任务结果完成即注入（含空闲态）——交接后新会话窗口靠这条拿到正文；
  批次汇总只补失败任务的错误正文，不再重复成功正文。
- B：get_all_active_tasks_with_details 在当前会话无任务时回退到最近派发任务的会话。

本测试不启动真实 executor/LLM，只驱动 manager 的完成链与状态查询。
"""
import importlib.util
from pathlib import Path


_MANAGER_PATH = Path(__file__).resolve().parents[2] / "plugins" / "system-subagent" / "manager.py"
_spec = importlib.util.spec_from_file_location("test_handoff_repro_manager", _MANAGER_PATH)
assert _spec is not None and _spec.loader is not None
_manager_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_manager_mod)
SubAgentManager = _manager_mod.SubAgentManager

_RESULT = "这是子任务的执行结果正文"


class _IdleBackend:
    """交接后新会话的引擎：is_streaming=False（继续指令那一轮已经结束）"""

    class _Engine:
        is_streaming = False

    chat_engine = _Engine()
    _hook_message_queue = None


class _BusyBackend(_IdleBackend):
    """对照：交接后的短流式仍在跑"""

    class _Engine:
        is_streaming = True

    chat_engine = _Engine()


class _ToolExecutor:
    def __init__(self, backend):
        self._backend = backend


class _FakeExecutor:
    """只提供完成链读取的字段，不启动线程"""

    def __init__(self, task_id, session_id):
        self.agent_name = "explore"
        self.task_description = "分析代码"
        self._execution_error = None
        self._task_session_id = session_id
        self._last_result = _RESULT
        self.last_result = _RESULT
        self.execution_error = None
        self.tool_call_count = 3
        self.start_time = 0

    def isFinished(self):
        return True

    def isRunning(self):
        return False

    def get_logs(self):
        return []

    def get_summary(self):
        return {"task_id": self._task_session_id, "agent_name": "explore"}


def _make_manager(backend):
    return SubAgentManager(
        agent_manager=None,
        tool_executor=_ToolExecutor(backend),
        get_llm_config=lambda: {},
    )


def _seed(mgr, task_id="t1", session_id="sess-A"):
    """会话 A 派发一个任务：批次计数 + 运行中条目 + 归档信息"""
    ex = _FakeExecutor(task_id, session_id)
    mgr._running_tasks[task_id] = ex
    mgr._batch_total = 1
    mgr._batch_completed = 0
    mgr._batch_task_ids = {task_id}
    mgr._finished_tasks[task_id] = {
        "result": _RESULT,
        "error": "",
        "agent_name": "explore",
        "task_description": "分析代码",
        "session_id": session_id,
    }
    mgr._last_task_session_id = session_id
    return ex


def test_idle_after_handoff_batch_carries_results():
    """A：引擎空闲时结果仍逐条注入，正文经单条进新会话（汇总已停发）"""
    mgr = _make_manager(_IdleBackend())
    _seed(mgr)

    injected = []
    mgr._dispatch_result_injection = lambda *a, **kw: injected.append((a, kw))
    batch_messages = []
    mgr.batch_finished.connect(lambda content, failed: batch_messages.append(content))

    mgr._on_executor_finished("t1", _RESULT)

    assert len(injected) == 1, "空闲态逐条注入，不攒到批次末尾"
    # 签名: (task_id, result, success, agent_name, description)
    assert injected[0][0][1] == _RESULT, "结果正文必须随单条注入送到新会话"
    assert batch_messages == [], "批次汇总已停发，不再开新轮"


def test_streaming_after_handoff_injects_single_task_only():
    """对照：流式中单任务逐条注入，全完成不再追加汇总"""
    mgr = _make_manager(_BusyBackend())
    _seed(mgr)

    injected = []
    mgr._dispatch_result_injection = lambda *a, **kw: injected.append((a, kw))
    batch_messages = []
    mgr.batch_finished.connect(lambda content, failed: batch_messages.append(content))

    mgr._on_executor_finished("t1", _RESULT)

    assert len(injected) == 1, "只有单任务注入，无汇总"
    assert injected[0][0][0] == "t1"
    assert injected[0][0][1] == _RESULT
    assert batch_messages == [], "流式路径不开新轮"


def test_status_finds_tasks_after_handoff():
    """B：新会话 B 无参 subagent_status 应回退查到会话 A 的任务"""
    mgr = _make_manager(_IdleBackend())
    mgr._last_task_session_id = "sess-A"
    mgr._finished_tasks["t1"] = {
        "result": _RESULT,
        "error": "",
        "agent_name": "explore",
        "task_description": "分析代码",
        "session_id": "sess-A",
    }
    mgr.set_current_session_id("sess-B")

    res = mgr.get_all_active_tasks_with_details()

    tasks = res.content["tasks"]
    assert any(t["task_id"] == "t1" for t in tasks), "新会话应回退查到旧会话任务"
    assert tasks[0].get("result") == _RESULT, "回退后结果正文仍可读"


def test_status_prefers_current_session_when_it_has_tasks():
    """B 边界：当前会话有任务时不回退，隔离语义保持"""
    mgr = _make_manager(_IdleBackend())
    mgr._last_task_session_id = "sess-A"
    mgr._finished_tasks["old"] = {
        "result": "旧结果", "error": "", "agent_name": "explore",
        "task_description": "旧任务", "session_id": "sess-A",
    }
    mgr._finished_tasks["new"] = {
        "result": "新结果", "error": "", "agent_name": "explore",
        "task_description": "新任务", "session_id": "sess-B",
    }
    mgr.set_current_session_id("sess-B")

    res = mgr.get_all_active_tasks_with_details()

    ids = {t["task_id"] for t in res.content["tasks"]}
    assert ids == {"new"}, "当前会话有任务时不得混入旧会话任务"


def test_explicit_task_id_query_still_unrestricted():
    """B 边界：带 task_id 的精确查询不依赖回退"""
    mgr = _make_manager(_IdleBackend())
    _seed(mgr)
    mgr.set_current_session_id("sess-B")

    res = mgr.get_tasks_status_with_details(["t1"])

    assert res.content["tasks"][0]["status"] == "finished"
