# -*- coding: utf-8 -*-
"""回归：子智能体执行失败被伪装成成功 + 流式读超时不重试

## 背景（用户报告）
会话卡显示绿色「✅ 执行结果」，正文却是「执行出错: The read operation timed out」；
主智能体收到的完成通知同样是 ✅。两层缺陷：

1. ``SubAgentExecutor.run()`` 里 ``_execute_agent_loop`` 的异常被就地转成
   ``result = f"执行出错: {str(e)}"``，``_execution_error`` 保持为空。完成链
   （``SubAgentManager._on_executor_finished``）按 ``_execution_error`` 判成功
   → DB 落 finished、会话卡 ✅、注入文案 ✅。
2. ``_make_api_call`` 的重试只包住 ``create_stream``（建流）。SSE 的读超时/
   断流发生在迭代期（``_aggregate_events``），异常直接穿透到 run()，重试从未发生。
   同时子智能体 client 读超时 120s，远短于主对话 600s，慢响应必死。

## 本测试验证
1. 迭代期 ReadTimeout → 重试（第二次成功），旧流被 close
2. loop 抛异常 → error_occurred 发射、finished_with_result 不发射、DB 落 error
3. stall 路径（executor 已从 running_tasks 移除）→ 完成链按失败结算，
   注入正文带错误原因而非「(无输出)」
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_MANAGER_PATH = _REPO_ROOT / "plugins" / "system-subagent" / "manager.py"


def _load_manager_mod():
    mod_name = "test_subagent_failure_paths_manager"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _MANAGER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── 流式重试 ──────────────────────────────────────────────────


class _FlakyStream:
    """第一次迭代抛读超时，之后正常产出内容；记录 close 调用。"""

    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.closed = False

    def __iter__(self):
        if self.fail:
            raise httpx.ReadTimeout("The read operation timed out")
        yield SimpleNamespace(
            type="content_delta",
            text="done",
            tool_call_id=None,
            name=None,
            index=0,
            arguments=None,
            usage=None,
        )

    def close(self) -> None:
        self.closed = True


class _FlakyTransport:
    id = "openai_chat"
    supports_streaming = True

    def __init__(self) -> None:
        self.streams = []

    def create_stream(self, *args, **kwargs):
        stream = _FlakyStream(fail=not self.streams)
        self.streams.append(stream)
        return stream


def _make_executor():
    from app.core.workers.subagent_worker import SubAgentExecutor

    return SubAgentExecutor(
        task_id="t-retry",
        agent_name="build",
        task_description="读文件",
        llm_config={"模型名称": "gpt-4", "API_KEY": "test-key", "API_URL": "https://example.com/v1"},
        agent_manager=None,
    )


def test_stream_read_timeout_retries_and_closes(monkeypatch):
    """迭代期读超时必须重试（此前只包住建流，超时直接判失败）。"""
    from app.core.workers.error_handler import retry_helper
    from app.plugins.registries.transport_registry import TransportRegistry

    monkeypatch.setattr(retry_helper.time, "sleep", lambda s: None)

    registry = TransportRegistry.get_instance()
    transport = _FlakyTransport()
    registry.register(transport, source="_test_fake_subagent_retry")
    try:
        ex = _make_executor()
        content, tool_calls, reasoning = ex._make_api_call([{"role": "user", "content": "hi"}])
    finally:
        registry.unregister_source("_test_fake_subagent_retry")

    assert len(transport.streams) == 2, f"读超时后应重试一次，实际建流 {len(transport.streams)} 次"
    assert transport.streams[0].closed, "失败流必须 close，否则重试泄漏 socket"
    assert content == "done"
    assert tool_calls == []
    assert reasoning == ""


# ── 失败结算 ──────────────────────────────────────────────────


class _FakeAgent:
    mode = "subagent"
    inherit_history = False
    inherit_history_count = None

    def is_subagent(self) -> bool:
        return True


class _FakeAgentManager:
    def __init__(self) -> None:
        self._agent = _FakeAgent()

    def get_agent(self, name):
        return self._agent

    def get_agent_system_prompt(self, *args, **kwargs):
        return "you are a subagent"

    def get_agent_tools_schema(self, *args, **kwargs):
        return []


def test_loop_error_settles_as_failure(monkeypatch):
    """loop 异常必须走 error 出口：不发射 finished_with_result，DB 落 error。"""
    from app.core.workers.subagent_worker import SubAgentExecutor

    ex = SubAgentExecutor(
        task_id="t-fail",
        agent_name="build",
        task_description="读文件",
        llm_config={},
        agent_manager=_FakeAgentManager(),
    )

    def _boom(*args, **kwargs):
        raise httpx.ReadTimeout("The read operation timed out")

    monkeypatch.setattr(ex, "_execute_agent_loop", _boom)

    saved = []
    ex._log_store_callback = lambda *args: saved.append(args)

    finished, errors = [], []
    ex.finished_with_result.connect(lambda tid, result: finished.append((tid, result)))
    ex.error_occurred.connect(lambda tid, err: errors.append((tid, err)))

    ex.run()

    assert not finished, "失败任务不得发射 finished_with_result（会被当成成功结算）"
    assert errors and "read operation timed out" in errors[0][1]
    assert saved, "_log_store_callback 应收到终态"
    status = saved[-1][3]
    assert status == "error", f"DB 终态应为 error，实际 {status}"
    assert "read operation timed out" in saved[-1][5]
    assert ex.execution_error, "execution_error 必须非空，完成链靠它判失败"


# ── stall 路径的失败结算 ──────────────────────────────────────


def test_stall_path_injects_failure_with_reason(monkeypatch):
    """stall 先删 running_tasks 再进完成链：success 不能因 executor=None 变成 True。"""
    manager_mod = _load_manager_mod()
    mgr = manager_mod.SubAgentManager(
        agent_manager=None,
        tool_executor=SimpleNamespace(_backend=None),
        get_llm_config=lambda: {},
    )

    task_id = "t-stalled"
    mgr._finished_tasks[task_id] = {
        "result": "",
        "error": "Task stalled: no log activity for 300s",
        "agent_name": "build",
        "task_description": "读文件",
        "session_id": "s1",
    }

    captured = {}
    monkeypatch.setattr(
        mgr,
        "_dispatch_result_injection",
        lambda tid, result, success, agent, desc: captured.update(
            task_id=tid, result=result, success=success, agent=agent
        ),
    )

    mgr._on_executor_finished(task_id, "")

    assert captured["success"] is False, "stall 任务不得按成功结算"
    assert "Task stalled" in captured["result"], "注入正文应带失败原因"
    assert captured["agent"] == "build", "executor 缺席时 agent_name 应回退归档"
