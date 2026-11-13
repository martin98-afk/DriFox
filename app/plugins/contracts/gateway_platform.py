# -*- coding: utf-8 -*-
"""Gateway 平台插件契约（万物即插件 Phase E）。

``plugins/<name>/gateways/<platform>.py`` 暴露 ``register(registry)``，注册本 def。
宿主与 system-im 只按 ``platform_id`` 查表，不认识任何具体平台，也不承载
任何平台配置字段——**配置由平台插件自己管**（``config_schema`` +
``PluginConfigStore``，或 ``plugin.config.read/write`` 能力）。

因此本契约只有「行为回调」，没有配置结构：

- ``adapter_factory()``：造适配器（适配器自己读自己的配置）
- ``is_enabled()``：当前是否启用（``start_all`` 据此跳过）
- ``set_enabled(v)``：改启用状态（设置卡/``platform.control`` 据此开关）
- ``validate()``：配置是否可用，返回 ``(ok, error)``
- ``check_requirements()``：依赖（SDK/deps）是否就绪
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple


@dataclass(frozen=True)
class GatewayPlatformDef:
    """一个平台适配器的完整声明。"""

    platform_id: str
    display_name: str
    adapter_factory: Callable[[], Any]  # () -> BasePlatformAdapter
    is_enabled: Callable[[], bool] = lambda: False
    set_enabled: Optional[Callable[[bool], None]] = None
    validate: Optional[Callable[[], Tuple[bool, str]]] = None
    check_requirements: Callable[[], bool] = lambda: True
    ui_order: int = 100
    icon_hint: str = ""
    source: str = ""

    @property
    def id(self) -> str:
        """runtime loader 覆盖判定用（对齐 ModelAdapter.id 约定）。"""
        return self.platform_id
