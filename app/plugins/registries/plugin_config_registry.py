# -*- coding: utf-8 -*-
"""插件配置注册表 — config schema 的进程级单例（E1）。

PluginManager 扫描 plugin.json 时注册；PluginConfigCard 渲染与
PluginConfigStore 默认值查询消费。热重载：rescan 覆盖同 plugin_name，
插件删除时 unregister_plugin 清理。
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional

from app.plugins.contracts.plugin_config import PluginConfigSchema


class PluginConfigRegistry:
    """plugin_name → PluginConfigSchema（注册序保持，同名覆盖）"""

    def __init__(self) -> None:
        self._schemas: Dict[str, PluginConfigSchema] = {}
        self._lock = threading.Lock()

    def register(self, schema: PluginConfigSchema) -> None:
        with self._lock:
            self._schemas[schema.plugin_name] = schema

    def get(self, plugin_name: str) -> Optional[PluginConfigSchema]:
        with self._lock:
            return self._schemas.get(plugin_name)

    def list_schemas(self) -> List[PluginConfigSchema]:
        with self._lock:
            return list(self._schemas.values())

    def unregister_plugin(self, plugin_name: str) -> None:
        with self._lock:
            self._schemas.pop(plugin_name, None)


def get_instance() -> PluginConfigRegistry:
    """进程级单例访问（与 ModelAdapterRegistry.get_instance 对齐的模块级入口）"""
    global _instance
    if _instance is not None:
        return _instance
    with _instance_lock:
        if _instance is None:
            _instance = PluginConfigRegistry()
    return _instance


_instance: Optional[PluginConfigRegistry] = None
_instance_lock = threading.Lock()

# 类级访问器（调用方统一 PluginConfigRegistry.get_instance()）
PluginConfigRegistry.get_instance = staticmethod(get_instance)
