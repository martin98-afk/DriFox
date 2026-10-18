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

    def _tool_read(host: Any):
        """工具注册表只读面（与 ui_core 的同名能力语义一致）。"""

        def api() -> dict:
            from app.tools.registry import ToolRegistry

            reg = ToolRegistry.get_instance()
            return {
                "names": reg.names,
                "list": reg.list,
                "schemas": reg.schemas,
                "get": reg.get,
                "has": reg.has,
                "groups": reg.group_map,
                "is_no_prune": reg.is_no_prune,
                "is_no_offload": reg.is_no_offload,
                "is_danger": reg.get_danger,
                "is_protected": reg.is_protected,
            }

        return api

    def _tool_inject(host: Any):
        """schema 过滤器注册面。"""

        def api() -> dict:
            from app.tools.registry import ToolRegistry

            reg = ToolRegistry.get_instance()
            return {
                "register_schema_filter": reg.register_schema_filter,
                "unregister_schema_filter": reg.unregister_schema_filter,
                "apply_schema_filters": reg.apply_schema_filters,
            }

        return api

    registry.register("state.kv", _kv, surface="tool", owner="core:tool")
    registry.register("tool.read", _tool_read, surface="tool", owner="core:tool")
    registry.register("tool.inject", _tool_inject, surface="tool", owner="core:tool")
    registry.register("lsp.query", _lsp, surface="tool", owner="core:tool")
    registry.register("mcp.call", _mcp, surface="tool", owner="core:tool")
    registry.register("net.upload", _gitee, surface="tool", owner="core:tool")
    registry.register("subagent.run", _subagent, surface="tool", owner="core:tool")
