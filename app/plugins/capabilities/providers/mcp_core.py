# -*- coding: utf-8 -*-
"""mcp.register 能力 provider：返回注册函数，插件经 caps_for 取来调用。

照 sync.register 的注册型能力范式：池值是一个闭包，插件调用它把 handler
反向登记进宿主 gateway。ctx.plugin 用于卸载时按插件清理。
"""
from __future__ import annotations

from typing import Any

from loguru import logger


def register(registry) -> None:
    registry.register("mcp.register", _provider, surface="ui", owner="core:mcp")


def _provider(host: Any, ctx: Any = None):
    plugin = getattr(ctx, "plugin", "") if ctx is not None else ""

    def register_server(server_name: str, handler, *, tools=None) -> bool:
        try:
            from app.core.services.local_mcp_gateway import get_gateway
            return get_gateway().register(server_name, handler, plugin=plugin, tools=tools)
        except Exception as e:
            logger.warning(f"[Capability:mcp.register] 注册 {server_name} 失败: {e}")
            return False

    return register_server
