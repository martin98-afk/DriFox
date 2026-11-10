# -*- coding: utf-8 -*-
"""LocalMcpGateway 单测：握手/工具调用/端口/按 plugin 注销。

Run: pytest tests/plugins/test_local_mcp_gateway.py -v
"""
import inspect
import json
import urllib.request

import pytest


@pytest.fixture
def gateway():
    from app.core.services.local_mcp_gateway import LocalMcpGateway

    gw = LocalMcpGateway()
    gw.start()
    yield gw
    gw.stop()


def _post(gw, payload, path="/mcp/browser"):
    req = urllib.request.Request(
        f"http://127.0.0.1:{gw.port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_initialize_returns_supported_version(gateway):
    status, body = _post(gateway, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}},
    })
    assert status == 200
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert "tools" in body["result"]["capabilities"]
    assert body["result"]["serverInfo"]["name"] == "browser"


def test_tools_list_and_call(gateway):
    gateway.register("browser", lambda req: {"text": "PONG"}, plugin="browser",
                     tools=[{"name": "ping", "description": "p",
                             "inputSchema": {"type": "object", "properties": {}}}])
    _, listed = _post(gateway, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [t["name"] for t in listed["result"]["tools"]] == ["ping"]
    _, called = _post(gateway, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                "params": {"name": "ping", "arguments": {}}})
    assert called["result"]["content"] == [{"type": "text", "text": "PONG"}]
    assert called["result"]["isError"] is False


def test_unknown_server_returns_jsonrpc_error(gateway):
    status, body = _post(gateway, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                         path="/mcp/nope")
    assert status == 200
    assert body["error"]["code"] == -32602


def test_port_is_dynamic_and_unregister(gateway):
    assert 1024 < gateway.port < 65536
    gateway.register("browser", lambda req: {"text": "x"}, plugin="browser", tools=[])
    gateway.unregister_plugin("browser")
    _, body = _post(gateway, {"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
                    path="/mcp/browser")
    assert "error" in body


def test_unload_hook_calls_unregister():
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    src = inspect.getsource(UIPluginRegistry.unload_plugin)
    assert "unregister_plugin" in src
