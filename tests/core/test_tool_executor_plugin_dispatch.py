# -*- coding: utf-8 -*-
"""
ToolExecutor 端到端分发测试（插件化工具系统）

覆盖（T2 计划 P3 + P4）：
P3 — 分发链路：
- 插件工具经 execute() 全链路执行，tool_ctx 注入 workdir/session_id/call_id/services/env
- impl 收到 args kwargs
- 旧风格 impl(**kwargs) 兼容（无 tool_ctx 参数）
- impl 返回 ToolResult 透传；返回 str/其他 → 包装为 ToolResult(True, str)
P4 — 异常路径：
- 未知工具 → "Unknown tool: xxx"（success=False）
- 缺 required 参数（registry schema 单一数据源）→ "Missing required arguments"
- impl 抛异常 → "Execution error: ..."（success=False）
- MCP 工具未连接 → "MCP 未连接"错误

运行: python -m pytest tests/core/test_tool_executor_plugin_dispatch.py -v
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
    """每个测试前重置 registry（测试用）"""
    ToolRegistry.reset_instance()
    yield
    ToolRegistry.reset_instance()


class _FakeBuiltinTools:
    """ToolExecutor.is_valid/_try_recover_mcp_prefix 所需的最小编成对象。

    非 QObject：not sip.isValid() 抛异常 → is_valid 走 except pass → True。
    _mcp_manager 提供 is_connected=False 的假 MCP 管理器（生产单例恒存在）。
    """

    def __init__(self, workdir="/wd"):
        self.workdir = workdir
        self._mcp_manager = _FakeMcpManager(connected=False)
        self._lsp_tools = None
        self.gitee_upload = None
        self._team_window_id = ""
        self._team_agent_name = ""


class _FakeMcpManager:
    def __init__(self, connected=False):
        self.is_connected = connected
        self._connections = {}


def _new_executor(**overrides):
    """轻量构造 ToolExecutor（__new__ 绕过 __init__，仅初始化 execute 路径依赖）。"""
    ex = object.__new__(__import__("app.core.tool_executor", fromlist=["ToolExecutor"]).ToolExecutor)
    ex._builtin_tools = overrides.get("builtin_tools", _FakeBuiltinTools())
    ex._backend = None  # 跳过 Pre/PostToolUse hook 阶段（不引入 backend 依赖）
    ex._lock = threading.Lock()
    ex._session_id = overrides.get("session_id", "sess-1")
    ex._call_id = overrides.get("call_id", "call-1")
    ex._workdir = overrides.get("workdir", "/wd")
    ex._workdir_user_set = True
    ex._sub_agent_manager = None
    ex._window_state = {}
    ex._window_state_lock = threading.Lock()
    ex._file_recorder = None
    return ex


def _register(name, schema, impl, **meta):
    """向 registry 注册插件工具"""
    return ToolRegistry.get_instance().register(
        name, schema, impl=impl, danger="safe", source="plugin:test", **meta
    )


def _schema(name, required=None):
    fn = {"name": name, "parameters": {"type": "object", "properties": {}}}
    if required:
        fn["parameters"]["required"] = required
    return {"type": "function", "function": fn}


class TestPluginDispatch:
    """P3：插件工具端到端分发"""

    def test_tool_ctx_injected(self):
        """tool_ctx 注入 workdir/session_id/call_id/services/env"""
        captured = {}

        def impl(tool_ctx, **kwargs):
            captured["ctx"] = tool_ctx
            return "ok"

        _register("t_ctx", _schema("t_ctx"), impl)
        ex = _new_executor(workdir="/proj", session_id="s9", call_id="c9")

        r = ex.execute("t_ctx", {})
        assert r.success
        ctx = captured["ctx"]
        assert ctx["workdir"] == "/proj"
        assert ctx["session_id"] == "s9"
        assert ctx["call_id"] == "c9"
        # services 注入窗口级状态容器（插件自包含关键）
        assert callable(ctx["services"]["window_state"]["get"])
        assert callable(ctx["services"]["window_state"]["set"])
        assert callable(ctx["services"]["window_state"]["delete"])
        # env 注入
        assert isinstance(ctx["env"], dict)
        assert "app_data_dir" in ctx["env"]

    def test_impl_receives_kwargs(self):
        """impl 收到 args 的 kwargs"""
        received = {}

        def impl(tool_ctx, **kwargs):
            received.update(kwargs)
            return "ok"

        _register("t_kw", _schema("t_kw"), impl)
        ex = _new_executor()
        ex.execute("t_kw", {"path": "a.py", "startline": 3})
        assert received == {"path": "a.py", "startline": 3}

    def test_legacy_impl_without_tool_ctx(self):
        """旧风格 impl(**kwargs) 兼容：签名无 tool_ctx 时不注入"""
        received = {}

        def impl(**kwargs):
            received.update(kwargs)
            return "legacy"

        _register("t_legacy", _schema("t_legacy"), impl)
        ex = _new_executor()
        r = ex.execute("t_legacy", {"x": 1})
        assert r.success
        assert r.content == "legacy"
        assert received == {"x": 1}

    def test_toolresult_passthrough(self):
        """impl 返回 ToolResult 透传（含 diff 等扩展字段）"""
        from app.tools.result import ToolResult

        def impl(tool_ctx, **kwargs):
            return ToolResult(True, content="done", diff="--- a/x\n+++ b/x\n+new")

        _register("t_tr", _schema("t_tr"), impl)
        ex = _new_executor()
        r = ex.execute("t_tr", {})
        assert r.success
        assert r.content == "done"
        assert r.diff == "--- a/x\n+++ b/x\n+new"

    def test_str_result_wrapped(self):
        """impl 返回 str → 包装为 ToolResult(True, str)"""
        _register("t_str", _schema("t_str"), lambda tool_ctx, **kw: "wrapped")
        ex = _new_executor()
        r = ex.execute("t_str", {})
        assert r.success
        assert r.content == "wrapped"

    def test_window_state_isolated_per_executor(self):
        """两个 executor 的 window_state 互不干扰（分发注入的是实例级容器）"""
        ex_a = _new_executor()
        ex_b = _new_executor()
        ex_a.window_state_set("k", "A")
        ex_b.window_state_set("k", "B")
        assert ex_a.window_state_get("k") == "A"
        assert ex_b.window_state_get("k") == "B"


class TestErrorPaths:
    """P4：异常路径"""

    def test_unknown_tool(self):
        """未知工具 → Unknown tool: xxx（success=False）"""
        ex = _new_executor()
        r = ex.execute("no_such_tool_xyz", {})
        assert not r.success
        assert "Unknown tool: no_such_tool_xyz" in r.error

    def test_missing_required_args(self):
        """registry schema required 缺失 → Missing required arguments"""
        def impl(tool_ctx, **kwargs):
            return "ok"

        _register("t_req", _schema("t_req", required=["command"]), impl)
        ex = _new_executor()
        r = ex.execute("t_req", {})
        assert not r.success
        assert "Missing required arguments" in r.error
        assert "command" in r.error

    def test_required_satisfied_passes(self):
        """required 参数齐全 → 正常执行（不误报缺失）"""
        def impl(tool_ctx, **kwargs):
            return f"ran {kwargs['command']}"

        _register("t_req2", _schema("t_req2", required=["command"]), impl)
        ex = _new_executor()
        r = ex.execute("t_req2", {"command": "ls"})
        assert r.success
        assert r.content == "ran ls"

    def test_impl_exception(self):
        """impl 抛异常 → Execution error（success=False）"""
        def impl(tool_ctx, **kwargs):
            raise ValueError("boom")

        _register("t_boom", _schema("t_boom"), impl)
        ex = _new_executor()
        r = ex.execute("t_boom", {})
        assert not r.success
        assert "Execution error" in r.error
        assert "boom" in r.error

    def test_mcp_not_connected(self):
        """mcp__ 工具未连接 → MCP 未连接错误"""
        ex = _new_executor()
        r = ex.execute("mcp__server__tool", {})
        assert not r.success
        assert "MCP" in r.error
