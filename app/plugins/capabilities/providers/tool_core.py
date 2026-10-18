# -*- coding: utf-8 -*-
"""工具 surface 的能力 provider（host = ToolExecutor）。

只注册工具插件实际需要的面：窗口级 KV、LSP、MCP、Gitee、子智能体。
"""

from __future__ import annotations

from typing import Any


def register(registry) -> None:
    def _kv(host: Any):
        return {
            "get": host.window_state_get,
            "set": host.window_state_set,
            "delete": host.window_state_delete,
        }

    def _lsp(host: Any):
        bt = getattr(host, "_builtin_tools", None)
        return getattr(bt, "_lsp_tools", None) if bt is not None else None

    def _mcp(host: Any):
        bt = getattr(host, "_builtin_tools", None)
        return getattr(bt, "_mcp_manager", None) if bt is not None else None

    def _gitee(host: Any):
        bt = getattr(host, "_builtin_tools", None)
        return getattr(bt, "gitee_upload", None) if bt is not None else None

    def _subagent(host: Any):
        return getattr(host, "_sub_agent_manager", None)

    registry.register("state.kv", _kv, surface="tool", owner="core:tool")
    registry.register("lsp.query", _lsp, surface="tool", owner="core:tool")
    registry.register("mcp.call", _mcp, surface="tool", owner="core:tool")
    registry.register("net.upload", _gitee, surface="tool", owner="core:tool")
    registry.register("subagent.run", _subagent, surface="tool", owner="core:tool")
