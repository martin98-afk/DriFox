# -*- coding: utf-8 -*-
"""session.send 窗口定向 + hook context 的 window_id 注入

背景（串窗 bug）：hook 内 ``caps_for`` 无 ctx，host 解析回落到「用户此刻选中的
窗口」。自动交接 hook 的补写提示因此被发进前台窗口——用户看到的是自己没触发
压缩的窗口突然开始写交接文档。修复靠两层：worker 在 hook context 里给出
``window_id``，session.send 按它定向入队。
"""

import queue
from types import SimpleNamespace

import pytest


def _tool_session_send(tool_core, host):
    """从 tool_core.register 取出 session.send provider（provider 是 register 内闭包）"""
    captured = {}

    class _Reg:
        def register(self, cap_id, provider, **kwargs):
            captured[cap_id] = provider

    tool_core.register(_Reg())
    return captured["session.send"](host)


def test_tool_surface_send_targets_given_window(monkeypatch):
    """tool surface 的 send(window_id=...) 入队到目标窗口，不进活跃窗口"""
    from app.core.conversation import action_queue
    from app.plugins.capabilities.providers import tool_core, ui_core

    q = action_queue.WindowActionQueue()
    monkeypatch.setattr(action_queue, "get_action_queue", lambda: q)

    active_backend = SimpleNamespace(_window_id="w-active")
    host = SimpleNamespace(_backend=active_backend, _window_id="w-active")

    other_backend = SimpleNamespace(_window_id="w-other")
    other_win = SimpleNamespace(backend=other_backend)
    monkeypatch.setattr(
        ui_core,
        "_resolve_window_by_id",
        lambda wid: other_win if str(wid) == "w-other" else None,
    )

    send = _tool_session_send(tool_core, host)
    assert send("补写交接", ephemeral=True, window_id="w-other") is True

    assert q.peek("w-active") == [], "不能落回活跃窗口（串窗根因）"
    items = q.peek("w-other")
    assert len(items) == 1
    assert items[0]["action"] == "send"
    assert items[0]["payload"]["text"] == "补写交接"
    assert items[0]["payload"]["ephemeral"] is True


def test_tool_surface_send_without_window_id_keeps_old_behavior(monkeypatch):
    """不传 window_id 时行为不变（工具调用点依赖 host 自己的窗口）"""
    from app.core.conversation import action_queue
    from app.plugins.capabilities.providers import tool_core

    q = action_queue.WindowActionQueue()
    monkeypatch.setattr(action_queue, "get_action_queue", lambda: q)

    backend = SimpleNamespace(_window_id="w1")
    host = SimpleNamespace(_backend=backend, _window_id="w1")

    send = _tool_session_send(tool_core, host)
    assert send("hello") is True
    assert q.peek("w1")[0]["payload"]["text"] == "hello"


def test_chat_worker_hook_context_carries_window_id(monkeypatch):
    """_trigger_worker_hook 的 ctx 必须带 backend 的 window_id（hook 定向依据）"""
    from app.core.hooks.hook_manager import HookManager
    from app.core.workers import chat_worker as cw

    captured = {}

    class _HM:
        def trigger_event(self, event_name, context=None, current_message="", **kwargs):
            captured.update(context or {})
            return []

    class _Backend:
        def __init__(self):
            self.hook_manager = _HM()
            self.tool_executor = None
            self._hook_message_queue = queue.Queue()
            self._window_id = "w-42"

        def get_current_session(self):
            return SimpleNamespace(session_id="s-1")

    worker = cw.OpenAIChatWorker.__new__(cw.OpenAIChatWorker)
    worker.tool_executor = SimpleNamespace(_backend=_Backend(), get_workdir=lambda: ".")
    worker._should_run_hook = lambda event: True
    worker._append_to_api_cache = lambda msgs: None
    worker._emit_with_callback = lambda *a, **k: None
    worker._current_session_messages = []
    worker._api_messages_cache = None

    monkeypatch.setattr(cw, "_check_team_member", lambda backend: False)
    monkeypatch.setattr(cw, "_check_team_leader", lambda backend: False)

    worker._trigger_worker_hook("Stop", [], [], extra_context={"reason": "completed"})

    assert captured.get("window_id") == "w-42"
    assert captured.get("session_id") == "s-1"


def test_subagent_hook_context_carries_window_id():
    """子智能体 hook ctx 同样带 window_id（缺它就会串到活跃窗口）"""
    from app.core.workers.subagent_worker import SubAgentExecutor

    worker = SubAgentExecutor.__new__(SubAgentExecutor)
    worker.agent_name = "build"
    worker.is_subagent_call = True
    worker.task_id = "t1"
    worker.task_description = "demo"
    worker.tool_executor = SimpleNamespace(
        _backend=SimpleNamespace(_window_id="w-sub"),
        get_workdir=lambda: ".",
    )

    ctx = worker._build_hook_context()
    assert ctx.get("window_id") == "w-sub"
    assert ctx.get("current_role") == "subagent"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
