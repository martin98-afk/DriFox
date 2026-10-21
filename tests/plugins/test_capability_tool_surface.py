# -*- coding: utf-8 -*-
"""工具面接入：ctx["capabilities"] 注入 / 旧 services 投影 / 声明裁剪 / 审计名不变。"""
import threading
from types import SimpleNamespace

import pytest


class _FakeBT:
    _lsp_tools = SimpleNamespace(ping=lambda: "lsp")
    _mcp_manager = SimpleNamespace(ping=lambda: "mcp")
    _team_window_id = "win-1"
    _team_agent_name = "agent-1"

    def gitee_upload(self, *a, **k):
        return "gitee"


def _make_executor(tmp_path):
    from app.core.tools.tool_executor import ToolExecutor

    ex = ToolExecutor.__new__(ToolExecutor)
    ex._builtin_tools = _FakeBT()
    ex._workdir = str(tmp_path)
    ex._call_id = "call-1"
    ex._sub_agent_manager = SimpleNamespace(anything="probe")
    ex._window_state = {}
    ex._window_state_lock = threading.Lock()
    return ex


def _run(executor, source, captured):
    from app.core.tools.tool_executor import ToolExecutor

    def impl(tool_ctx=None, **kwargs):
        captured.append(tool_ctx)
        return "ok"

    reg = SimpleNamespace(
        impl=impl,
        source=source,
        schema={"function": {"name": "cap_tool", "parameters": {"type": "object", "properties": {}}}},
    )
    return ToolExecutor._execute_registered_tool(executor, reg, {}, "sess-1")()


def _fake_pm(monkeypatch, manifest):
    from app.plugins.managers.plugin_manager import PluginManager

    fake = SimpleNamespace(get_plugin=lambda name: SimpleNamespace(manifest=manifest, path=""))
    monkeypatch.setattr(PluginManager, "get_instance", classmethod(lambda cls: fake))


@pytest.fixture(autouse=True)
def _fresh_registry():
    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    yield
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()


def test_capabilities_injected(tmp_path):
    ex = _make_executor(tmp_path)
    captured = []
    _run(ex, "plugin:cap-plug", captured)
    caps = captured[0]["capabilities"]
    assert "state.kv" in caps
    assert "window_state" not in caps  # 池用命名空间 ID
    assert caps["state.kv"]["get"]("nope", 42) == 42


def test_undeclared_full_grant(tmp_path, monkeypatch, log_capture):
    _fake_pm(monkeypatch, {})
    ex = _make_executor(tmp_path)
    captured = []
    _run(ex, "plugin:cap-plug", captured)
    caps = captured[0]["capabilities"]
    assert {"lsp.query", "mcp.call", "net.upload", "subagent.run"} <= set(caps.names())
    assert any("按旧语义全量注入" in r for r in log_capture)


def test_declared_trims_legacy_services(tmp_path, monkeypatch):
    _fake_pm(monkeypatch, {"capabilities": ["lsp"]})
    ex = _make_executor(tmp_path)
    captured = []
    _run(ex, "plugin:cap-plug", captured)
    ctx = captured[0]
    assert set(ctx["services"].keys()) == {"window_state", "lsp"}
    assert ctx["sub_agent_manager"] is None


def test_empty_declared_keeps_state_kv_only(tmp_path, monkeypatch):
    _fake_pm(monkeypatch, {"capabilities": []})
    ex = _make_executor(tmp_path)
    captured = []
    _run(ex, "plugin:cap-plug", captured)
    assert set(captured[0]["services"].keys()) == {"window_state"}


def test_gitee_audit_log_name_unchanged(tmp_path, log_capture):
    ex = _make_executor(tmp_path)
    captured = []
    _run(ex, "plugin:audit-plug", captured)
    captured[0]["services"]["gitee"]()
    assert any("[ServiceAudit]" in r and "service=gitee" in r for r in log_capture)


def test_app_data_dir_always_present(tmp_path, monkeypatch):
    _fake_pm(monkeypatch, {"capabilities": []})
    ex = _make_executor(tmp_path)
    captured = []
    _run(ex, "plugin:cap-plug", captured)
    assert captured[0]["env"].get("app_data_dir")
