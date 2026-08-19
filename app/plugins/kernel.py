# -*- coding: utf-8 -*-
"""插件组件加载内核 — 组件注册与热重载分派的单一事实源。

设计动机（万物为插件）：新增组件类型只需在 KNOWN_COMPONENTS 登记目录名，
并注册对应 reloader（ComponentReloaderRegistry），backend 不再需要改动。
backend 的 _identify_all_components_from_changes / fallback 与
_reload_single_plugin 的 8 分支 if 全部改为查本模块。
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Set

from loguru import logger

# 组件名集合：插件目录下的物理子目录/根文件 → 组件类型。
# ⚠️ 与 plugin_manager._scan_plugins 的目录探测保持一致，
#    新增组件类型时两处同步（后续 Task 会把 plugin_manager 也改为引用此处）。
KNOWN_COMPONENTS: Set[str] = {
    "agents",
    "hooks",
    "commands",
    "themes",
    "skills",
    "mcp",
    "lsp",
    "ui",
    "tools",
    "providers",
    "team_templates",
}

# 插件根目录的关键文件 → 组件类型（.mcp.json/.lsp.json 位于插件根而非子目录）
ROOT_FILE_COMPONENTS: Dict[str, str] = {
    ".mcp.json": "mcp",
    ".lsp.json": "lsp",
}


def validate_component(name: str) -> bool:
    """校验组件名是否在册（未知组件返回 False，调用方跳过重载）"""
    return name in KNOWN_COMPONENTS
