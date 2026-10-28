# -*- coding: utf-8 -*-
"""ToolResult.end_turn 插件通用契约 — 工具请求本轮收尾

契约：插件工具返回 ToolResult(end_turn=True) 表示本次返回后不再发起下一轮 API
调用。主 worker 与子智能体 worker 同一语义；任何插件都能用（批处理跑完整批、
脚手架建完项目、移交类工具写完即走）。

动机（首个使用方 system-handoff）：交接工具只往动作队列登记「停流 → 新建 →
发继续指令」，drain 发生在 _on_stream_finished（本轮真正结束）。模型写完交接常
继续调工具，本轮迟迟不结束 → 交接全部延后，实测白跑好几轮工具。
"""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject


def _make_worker(result_obj):
    """最小可用 worker：__new__ 绕过 __init__，仅注入 _execute_tool 依赖的状态。"""
    worker_cls = pytest.importorskip("app.core.workers.chat_worker").OpenAIChatWorker
    w = worker_cls.__new__(worker_cls)
    QObject.__init__(w)  # 不补 QObject 初始化时 Signal 描述器会报父类未初始化
    w._backup_delete_targets = lambda arguments: None
    w._should_run_hook = lambda ev: True
    w._is_cancelled = False
    w._tool_execution_cancelled = False
    w._emit_with_callback = lambda *a, **k: None
    w.tool_executor = SimpleNamespace(_backend=None, execute=lambda *a, **k: result_obj)
    return w


def test_end_turn_requests_turn_end():
    """ToolResult(end_turn=True) → 本轮收尾"""
    from app.tools.result import ToolResult

    w = _make_worker(ToolResult(True, content="ok", end_turn=True))
    w._execute_tool("update_handoff", {"content": "x", "switch": True}, "call_1")
    assert w._end_turn_requested is True


def test_plain_tool_keeps_turn_running():
    """普通工具结果不触发收尾"""
    from app.tools.result import ToolResult

    w = _make_worker(ToolResult(True, content="ok"))
    w._execute_tool("read", {"path": "a.py"}, "call_2")
    assert getattr(w, "_end_turn_requested", False) is False


def test_missing_end_turn_attribute_does_not_crash():
    """老工具返回非 ToolResult 对象（无 end_turn 属性）时安全跳过"""
    w = _make_worker(SimpleNamespace(success=True, content="ok"))
    w._execute_tool("bash", {"command": "ls"}, "call_3")
    assert getattr(w, "_end_turn_requested", False) is False


def test_end_turn_serialized_in_to_dict():
    """to_dict 带 end_turn：跨进程/网关传输也不丢契约"""
    from app.tools.result import ToolResult

    assert ToolResult(True, content="ok", end_turn=True).to_dict()["end_turn"] is True
    assert "end_turn" not in ToolResult(True, content="ok").to_dict()
