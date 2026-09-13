# -*- coding: utf-8 -*-
"""插件组件加载内核 — 组件注册与热重载分派的单一事实源。

设计动机（万物为插件）：新增组件类型只需在 KNOWN_COMPONENTS 登记目录名，
并注册对应 reloader（ComponentReloaderRegistry），backend 不再需要改动。
backend 的 _identify_all_components_from_changes / fallback 与
_reload_single_plugin 的 8 分支 if 全部改为查本模块。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Set

from loguru import logger

# 组件名集合：插件目录下的物理子目录/根文件 → 组件类型。
# ⚠️ KNOWN_COMPONENTS 为超集：_scan_plugins 检测集 ∪ backend 路径识别集 ∪
#    独立 loader（tools 由 plugin_tool_loader 独立扫描，不在 _scan_plugins 检测集内）。
#    新增组件类型时按来源在对应处登记（后续 Task 会把 plugin_manager 也改为引用此处）。
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
    "model_adapters",
    "loop_policies",
    "hook_policies",
    "storages",
    "serializers",
    "gateways",
    "engines",
}

# 组件优先级元组（用于多组件批处理中决定先后顺序，与旧 backend._COMPONENT_ORDER dict 数值一致）
# 必须为 tuple 而非 set：set 遍历时序不确定，backend.reload_plugin_subsystems 增量段按此排序遍历，
# 漏掉类型会导致删除清理段跳过组件。agents 最先：它会影响 commands 和 hooks 同步。
COMPONENT_ORDER: tuple = (
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
    "model_adapters",
    "loop_policies",
    "hook_policies",
    "storages",
    "serializers",
    "gateways",
    "engines",
)

# 插件根目录的关键文件 → 组件类型（.mcp.json/.lsp.json 位于插件根而非子目录）
ROOT_FILE_COMPONENTS: Dict[str, str] = {
    ".mcp.json": "mcp",
    ".lsp.json": "lsp",
    # .drifox-plugin/ 是插件清单目录（plugin.json 等），其变更意味着组件清单可能增删
    # → 映射到 sentinel "__manifest__"，触发 backend 遍历该插件全部已声明 components 全量重载
    # （不在 KNOWN_COMPONENTS 内，registry 无对应 reloader — 特殊值由 _reload_single_plugin 拦截处理）
    ".drifox-plugin": "__manifest__",
}


def validate_component(name: str) -> bool:
    """校验组件名是否在册（未知组件返回 False，调用方跳过重载）"""
    return name in KNOWN_COMPONENTS


@dataclass
class ReloadContext:
    """一次组件重载的上下文（reloader 的唯一入参）

    plugin: PluginInfo | None — 重载后插件信息；插件被删除时为 None
    （reloader 据此走清理分支而非重载分支）
    """

    plugin_name: str
    plugin: Any
    component: str
    is_new_plugin: bool


class ComponentReloaderRegistry:
    """组件 reloader 注册表 — 组件名 → 重载函数

    backend 的 _reload_single_plugin 8 分支 if 将重构为查本表分派。
    新增组件类型 = 注册一个 reloader，backend 零改动（万物为插件的骨架扩展点）。
    reloader 返回 bool | int（agents 返回数量，其余 True/False），仅用于结果上报。
    """

    def __init__(self) -> None:
        self._reloaders: Dict[str, Callable[[ReloadContext], Any]] = {}
        self._lock = threading.Lock()

    def register(self, component: str, reloader: Callable[[ReloadContext], Any]) -> None:
        """注册组件 reloader（重复注册覆盖 — 支持插件替换内置 reloader）"""
        with self._lock:
            if component in self._reloaders:
                logger.warning(f"[kernel] reloader 覆盖注册: {component}")
            self._reloaders[component] = reloader

    def reload(self, ctx: ReloadContext) -> Any:
        """按组件名分派重载；未注册返回 False（调用方记入 result 跳过）"""
        with self._lock:
            reloader = self._reloaders.get(ctx.component)
        if reloader is None:
            return False
        try:
            return reloader(ctx)
        except Exception as e:
            logger.error(f"[kernel] reloader '{ctx.component}' 执行失败 ({ctx.plugin_name}): {e}")
            return False

    def known_components(self) -> Set[str]:
        with self._lock:
            return set(self._reloaders.keys())


_registry: Optional[ComponentReloaderRegistry] = None
_registry_lock = threading.Lock()


def get_reloader_registry() -> ComponentReloaderRegistry:
    """进程级单例（backend 主线程分派用）"""
    global _registry
    if _registry is not None:
        return _registry
    with _registry_lock:
        if _registry is None:
            _registry = ComponentReloaderRegistry()
        return _registry
