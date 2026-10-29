# -*- coding: utf-8 -*-
"""按 surface 幂等注册能力 provider。

调用点：ToolExecutor._execute_registered_tool（tool）/ MainWidget._build_ui_services（ui）。
provider 是 host 无关的纯函数，因此只需注册一次，不随窗口重复。
"""

from __future__ import annotations

from typing import Dict

_READY: Dict[str, bool] = {"ui": False, "tool": False}


def ensure_registered(surface: str) -> None:
    if _READY.get(surface):
        return
    from app.plugins.capabilities.registry import CapabilityRegistry

    registry = CapabilityRegistry.get_instance()
    if surface == "tool":
        from app.plugins.capabilities.providers import tool_core

        tool_core.register(registry)
    elif surface == "ui":
        from app.plugins.capabilities.providers import history_core, platform, ui_core

        ui_core.register(registry)
        history_core.register(registry)
        platform.register_ui(registry)
    else:
        raise ValueError(f"未知 surface: {surface}")
    _READY[surface] = True


def reset_bootstrap() -> None:
    """测试用：清空就绪标记（配合 CapabilityRegistry.reset_instance）。"""
    _READY["ui"] = False
    _READY["tool"] = False
