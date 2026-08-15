# -*- coding: utf-8 -*-
"""
ToolExecutor MCP 分发测试

覆盖（T2 计划 P10）：
- _try_recover_mcp_prefix：唯一命中补全前缀 / 歧义放弃返回 None / 已有前缀原样 / 未连接返回 None
- execute mcp__ 路由：未连接 → "MCP 未连接"错误
- execute mcp__ 路由：连接成功 → 调 mcp_manager.call_tool_sync 透传结果

运行: python -m pytest tests/core/test_executor_mcp_dispatch.py -v
"""
import os
import sys
import threading
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def fresh_registry():
    ToolRegistry.reset_instance()
    yield
    ToolRegistry.reset_instance()


class _FakeMcpConn:
    def __init__(self, tools, enabled=True, session=True):
        self.tools = tools  # [SimpleNamespace(name=...)]
        self.enabled = enabled
        self.session = session


class _FakeMcpManager:
    TOOL_PREFIX = "mcp__"

    def __init__(self, connections=None, connected=True, call_result=None):
        self._connections = connections or {}
        self.is_connected = connected
        self.call_result = call_result  # ToolResult 或异常

    def call_tool_sync(self, tool_name, args):
        if isinstance(self.call_result, Exception):
            raise self.call_result
        return self.call_result


class _FakeBuiltinTools:
    def __init__(self, mcp_manager):
        self.workdir = "/wd"
        self._mcp_manager = mcp_manager
        self._lsp_tools = None
        self.gitee_upload = None
        self._team_window_id = ""
        self._team_agent_name = ""


def _new_executor(mcp_manager):
    from types import SimpleNamespace

    bt = _FakeBuiltinTools(mcp_manager)
    ex = object.__new__(__import__("app.core.tool_executor", fromlist=["ToolExecutor"]).ToolExecutor)
    ex._builtin_tools = bt
    ex._backend = None
    ex._lock = threading.Lock()
    ex._session_id = "s"
    ex._call_id = "c"
    ex._workdir = "/wd"
    ex._workdir_user_set = True
    ex._sub_agent_manager = None
    ex._window_state = {}
    ex._window_state_lock = threading.Lock()
    ex._file_recorder = None
    return ex


class TestTryRecoverMcpPrefix:
    """P10：_try_recover_mcp_prefix"""

    def _conn(self, names):
        from types import SimpleNamespace

        return _FakeMcpConn([SimpleNamespace(name=n) for n in names])

    def test_unique_match_recovers_prefix(self):
        """裸工具名唯一命中 → 补全 mcp__server__tool"""
        mgr = _FakeMcpManager(connections={"srv1": self._conn(["read"])})
        ex = _new_executor(mgr)
        assert ex._try_recover_mcp_prefix("read") == "mcp__srv1__read"

    def test_server_tool_form_recovers(self):
        """'server__tool'（缺前缀但带 server）→ 补全"""
        mgr = _FakeMcpManager(connections={"srv2": self._conn(["write"])})
        ex = _new_executor(mgr)
        assert ex._try_recover_mcp_prefix("srv2__write") == "mcp__srv2__write"

    def test_ambiguous_returns_none(self):
        """同名工具多个 server → 歧义放弃（返回 None，走失败路径）"""
        mgr = _FakeMcpManager(connections={
            "srv_a": self._conn(["dup"]),
            "srv_b": self._conn(["dup"]),
        })
        ex = _new_executor(mgr)
        assert ex._try_recover_mcp_prefix("dup") is None

    def test_already_prefixed_unchanged(self):
        """已有 mcp__ 前缀 → 原样返回"""
        mgr = _FakeMcpManager(connections={"srv1": self._conn(["read"])})
        ex = _new_executor(mgr)
        assert ex._try_recover_mcp_prefix("mcp__srv1__read") == "mcp__srv1__read"

    def test_not_connected_returns_none(self):
        """未连接 → 返回 None（不尝试恢复）"""
        mgr = _FakeMcpManager(connected=False)
        ex = _new_executor(mgr)
        assert ex._try_recover_mcp_prefix("read") is None

    def test_empty_returns_none(self):
        """空工具名 → None"""
        mgr = _FakeMcpManager(connections={"srv1": self._conn(["read"])})
        ex = _new_executor(mgr)
        assert ex._try_recover_mcp_prefix("") is None


class TestExecuteMcpRouting:
    """P10：execute mcp__ 路由"""

    def test_not_connected_returns_error(self):
        """未连接 → MCP 未连接错误"""
        mgr = _FakeMcpManager(connected=False)
        ex = _new_executor(mgr)
        r = ex.execute("mcp__srv__tool", {})
        assert not r.success
        assert "MCP 未连接" in r.error

    def test_connected_dispatches_and_returns_result(self):
        """连接成功 → call_tool_sync 透传 ToolResult"""
        from app.tools.result import ToolResult

        mgr = _FakeMcpManager(connected=True, call_result=ToolResult(True, content="mcp-ok"))
        ex = _new_executor(mgr)
        r = ex.execute("mcp__srv__tool", {"q": 1})
        assert r.success
        assert r.content == "mcp-ok"

    def test_connected_call_exception_returns_error(self):
        """call_tool_sync 抛异常 → 执行异常错误"""
        mgr = _FakeMcpManager(connected=True, call_result=RuntimeError("mcp boom"))
        ex = _new_executor(mgr)
        r = ex.execute("mcp__srv__tool", {})
        assert not r.success
        assert "MCP" in r.error or "异常" in r.error
