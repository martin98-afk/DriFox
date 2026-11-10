# -*- coding: utf-8 -*-
"""回归：子智能体 error/stall 路径绕过完成链，批次汇总永不触发

背景 bug（用户报告）：子智能体「偶尔无法执行，执行完、报错都没有注入消息」。

根因：SubAgentExecutor.run() 有三条出口——
- finished_with_result.emit → manager._on_executor_finished（完成链：批次计数+汇总）
- error_occurred.emit       → 无人监听（subagent_para 传 on_error=None）
- agent not found 裸 return  → 连信号都不发

只要批次内任一任务走 error/stall/cancel 出口，_batch_completed 永远凑不齐
_batch_total → 批次汇总（非流式下唯一的通知通道）永不触发，UI 面板行也
收不了尾（task_finished 不发）。非流式场景连单条 hook 注入都没有，
结果就是「任务明明结束了，对话里永远等不到通知」。

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
        return {"task_id": task_id_local, "agent_name": "explore"}

def _make_manager():
    return SubAgentManager(
        agent_manager=None,
        tool_executor=_ToolExecutor(_IdleBackend()),
        get_llm_config=lambda: {},
    )

def _seed_batch(mgr, task_ids):
    """派发一个 N 任务批次：批次计数 + 运行中条目"""
    mgr._batch_total = len(task_ids)
    mgr._batch_completed = 0
    mgr._batch_task_ids = set(task_ids)
    for tid in task_ids:
        mgr._running_tasks[tid] = _FakeExecutor(tid)
    return mgr

def test_error_task_blocks_batch_summary_forever():
    """回归：批次内 t1 走 error 出口，t2 正常完成。

    修复语义：error_occurred 经 manager._on_executor_error 进完成链结算，
    批次汇总触发且带 t1 的失败标记与错误正文。
    """
    mgr = _make_manager()
    _seed_batch(mgr, ["t1", "t2"])

    batch_messages = []
    mgr.batch_finished.connect(lambda content, failed: batch_messages.append((content, failed)))

    # t1 线程出错：error_occurred.emit → 修复后由 _on_executor_error 接住
    mgr._on_executor_error("t1", _ERR_MSG)
    # t2 正常完成：finished_with_result.emit → _on_executor_finished
    mgr._on_executor_finished("t2", _RESULT_OK)

    assert batch_messages, "error 任务必须被完成链结算，批次汇总不能永久缺席"
    content, failed = batch_messages[0]
    assert "t1" in content, "批次汇总必须包含 error 任务的条目"
    assert _ERR_MSG in content, "非流式汇总必须带 error 任务的错误正文"
    assert failed == 1, "失败计数应反映 error 任务"

def test_error_then_late_finished_signal_is_idempotent():
    """同一线程先发 error_occurred、后发 finished_with_result（cancel 后退出）：
    第二次信号必须被 _settled_tasks 拦住，批次计数不超发。"""
    mgr = _make_manager()
    _seed_batch(mgr, ["t1", "t2"])
    batch_messages = []
    mgr.batch_finished.connect(lambda content, failed: batch_messages.append(content))

    mgr._on_executor_error("t1", _ERR_MSG)
    mgr._on_executor_finished("t1", "迟到的完成信号")  # 幂等：应被拦
    mgr._on_executor_finished("t2", _RESULT_OK)

    assert len(batch_messages) == 1, "批次汇总只触发一次"
    assert "迟到的完成信号" not in batch_messages[0]

def test_stall_task_blocks_batch_summary():
    """回归变体：t1 被 stall 检测器取消。

    修复语义：stall 路径手工归档后调 _on_executor_finished 统一结算
    （批次计数补齐），t2 完成后批次汇总正常触发。
    """
    mgr = _make_manager()
    _seed_batch(mgr, ["t1", "t2"])
    # 模拟 stall 路径（与修复后 _check_stalled_tasks 一致：归档 + 统一结算）
    mgr._finished_tasks["t1"] = {
        "result": "",
        "error": "Task stalled: no log activity for 300s (timeout=300s)",
        "agent_name": "explore",
        "task_description": "任务 t1",
        "session_id": "sess-1",
    }
    del mgr._running_tasks["t1"]
    mgr._on_executor_finished("t1", "")

    batch_messages = []
    mgr.batch_finished.connect(lambda content, failed: batch_messages.append(content))

    mgr._on_executor_finished("t2", _RESULT_OK)

    assert batch_messages, "stall 任务必须被完成链结算，批次汇总不能永久缺席"

def test_all_error_batch_still_notifies():
    """复现变体：整批全灭（如代理配置错误导致全部秒挂）。

    期望：批次汇总照常触发并逐条带错误正文——模型至少要知道任务全挂了。
    """
    mgr = _make_manager()
    _seed_batch(mgr, ["t1", "t2"])
    for tid in ("t1", "t2"):
        mgr._running_tasks[tid]._execution_error = _ERR_MSG

    batch_messages = []
    mgr.batch_finished.connect(lambda content, failed: batch_messages.append(content))

    # error 出口走统一结算后的行为：两个任务都应被结算并触发汇总
    mgr._on_executor_finished("t1", "")
    mgr._on_executor_finished("t2", "")

    assert batch_messages, "全 error 批次也必须触发汇总通知"
