# -*- coding: utf-8 -*-
"""A3：工具面 capability 声明制 + schema 规范化 + 高权限服务审计。

七用例（T8.1 行 3 口径）：默认全注入+审计断言 / 声明裁剪 / 空数组全裁 /
app_data_dir 恒在 / name 不匹配 warning / 超长截断 / 服务调用审计断言。
全程 tmp_path，无网络无真实插件目录写入。
"""
import threading
from types import SimpleNamespace

import pytest
from loguru import logger

from app.core.tool_executor import ToolExecutor
from app.tools.registry import ToolRegistry


@pytest.fixture()
def log_capture():
    """loguru WARNING+ 捕获为文本列表。"""
    records = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    yield records
    logger.remove(sink_id)


class _FakeBT:
    """BuiltinTools 最小桩（services 注入来源）。"""

    _lsp_tools = SimpleNamespace(ping=lambda: "lsp")
    _mcp_manager = SimpleNamespace(ping=lambda: "mcp")
    _team_window_id = "win-1"
    _team_agent_name = "agent-1"

    def gitee_upload(self, *args, **kwargs):
        return "gitee"


class _FakeSubAgentManager:
    anything = "probe"


def _make_executor(tmp_path) -> ToolExecutor:
    """跳过 __init__ 的最小 ToolExecutor（_execute_registered_tool 依赖面）。"""
    executor = ToolExecutor.__new__(ToolExecutor)
    executor._builtin_tools = _FakeBT()
    executor._workdir = str(tmp_path)
    executor._call_id = "call-1"
    executor._sub_agent_manager = _FakeSubAgentManager()
    executor._window_state = {}
    executor._window_state_lock = threading.Lock()
    return executor


def _run_tool(executor: ToolExecutor, source: str, captured: list):
    """注册一个捕获 tool_ctx 的最小工具并执行，返回 (ctx, ToolResult)。"""

    def impl(tool_ctx=None, **kwargs):
        captured.append(tool_ctx)
        return "ok"

    reg = SimpleNamespace(
        impl=impl,
        source=source,
        schema={"function": {"name": "cap_tool", "parameters": {"type": "object", "properties": {}}}},
    )
    run = executor._execute_registered_tool(reg, {}, "sess-1")
    result = run()
    assert len(captured) == 1
    return captured[0], result


def _fake_pm(monkeypatch, manifest):
    """monkeypatch PluginManager.get_instance → get_plugin 返回带 manifest 的桩。"""
    from app.plugins.managers.plugin_manager import PluginManager

    fake_pm = SimpleNamespace(get_plugin=lambda name: SimpleNamespace(manifest=manifest))
    monkeypatch.setattr(PluginManager, "get_instance", classmethod(lambda cls: fake_pm))


def test_default_injects_all_services_and_audits(tmp_path, log_capture):
    """未声明 capabilities → services 全量注入（现状不变），触达即审计。"""
    executor = _make_executor(tmp_path)
    captured = []
    ctx, result = _run_tool(executor, "plugin:cap-plug", captured)
    assert result.success
    assert set(ctx["services"].keys()) >= {"lsp", "mcp", "gitee", "window_state"}
    assert ctx["sub_agent_manager"] is not None
    # 未触达前不产生审计
    assert not any("[ServiceAudit]" in r for r in log_capture)
    # 触达 gitee → 一行审计
    ctx["services"]["gitee"]()
    audits = [r for r in log_capture if "[ServiceAudit]" in r]
    assert len(audits) == 1
    assert "plugin=cap-plug" in audits[0] and "service=gitee" in audits[0]


def test_declared_capabilities_trim_services(tmp_path, log_capture, monkeypatch):
    """声明 ["lsp"] → services 裁到 lsp+window_state，sub_agent_manager 置 None。"""
    _fake_pm(monkeypatch, {"capabilities": ["lsp"]})
    executor = _make_executor(tmp_path)
    captured = []
    ctx, _ = _run_tool(executor, "plugin:cap-plug", captured)
    assert set(ctx["services"].keys()) == {"lsp", "window_state"}
    assert ctx["sub_agent_manager"] is None


def test_empty_capabilities_trim_all(tmp_path, log_capture, monkeypatch):
    """空数组 → 高权限服务全裁（仅保留窗口级 KV），sub_agent_manager 置 None。"""
    _fake_pm(monkeypatch, {"capabilities": []})
    executor = _make_executor(tmp_path)
    captured = []
    ctx, _ = _run_tool(executor, "plugin:cap-plug", captured)
    assert set(ctx["services"].keys()) == {"window_state"}
    assert ctx["sub_agent_manager"] is None


def test_app_data_dir_always_present(tmp_path, log_capture, monkeypatch):
    """capability 裁剪不影响 env：app_data_dir 恒保留。"""
    _fake_pm(monkeypatch, {"capabilities": []})
    executor = _make_executor(tmp_path)
    captured = []
    ctx, _ = _run_tool(executor, "plugin:cap-plug", captured)
    assert ctx["env"].get("app_data_dir")


def test_schema_name_mismatch_warns_and_fixed(tmp_path, log_capture):
    """function.name 与注册名不一致 → 剔除修正为注册名 + warning，不拒载。"""
    reg = ToolRegistry.get_instance()
    original = {"function": {"name": "wrong_name", "parameters": {"type": "object", "properties": {}}}}
    try:
        ok = reg.register(
            "a3_name_tool",
            original,
            impl=lambda: "ok",
            danger="safe",
            source="plugin:some-plug",
        )
        assert ok is True
        stored = reg.get("a3_name_tool")
        assert stored.schema["function"]["name"] == "a3_name_tool"
        # 调用方原始 dict 不被污染（规范化在副本上）
        assert original["function"]["name"] == "wrong_name"
        assert any("[SchemaGuard]" in r and "a3_name_tool" in r for r in log_capture)
    finally:
        reg.unregister("a3_name_tool")


def test_description_over_2kb_truncated(tmp_path, log_capture):
    """description 超 2KB → 截断到 2048 + warning，不拒载。"""
    reg = ToolRegistry.get_instance()
    schema = {
        "function": {
            "name": "a3_desc_tool",
            "parameters": {"type": "object", "properties": {}},
            "description": "x" * 3000,
        }
    }
    try:
        ok = reg.register("a3_desc_tool", schema, impl=lambda: "ok", danger="safe", source="plugin:some-plug")
        assert ok is True
        stored = reg.get("a3_desc_tool")
        assert len(stored.schema["function"]["description"]) == 2048
        assert any("[SchemaGuard]" in r and "截断" in r for r in log_capture)
    finally:
        reg.unregister("a3_desc_tool")


def test_service_call_audited(tmp_path, log_capture):
    """高权限服务实际触达（属性访问/调用）记 [ServiceAudit]（未声明 capabilities 场景）。"""
    executor = _make_executor(tmp_path)
    captured = []
    ctx, _ = _run_tool(executor, "plugin:audit-plug", captured)
    _ = ctx["services"]["mcp"].ping  # 属性触达即审计
    _ = ctx["sub_agent_manager"].anything
    audits = [r for r in log_capture if "[ServiceAudit]" in r]
    assert any("plugin=audit-plug" in r and "service=mcp" in r for r in audits)
    assert any("plugin=audit-plug" in r and "service=subagent" in r for r in audits)
