# -*- coding: utf-8 -*-
"""Gateway 平台插件契约（万物即插件 Phase E）。

plugins/<name>/gateways/<platform>.py 暴露 register(registry)，注册本 def。
主程序 PlatformManager / GatewayConfigHelper / gateway_setting_card 全部
查 GatewayPlatformRegistry，不再出现平台 if-elif 分支。
内置平台插件（plugins/system/gateways/）config_builder 闭包读主程序
Settings（存量用户配置零迁移）；第三方平台建议经 E1 config_schema +
PluginConfigStore 提供配置。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple

if TYPE_CHECKING:  # 仅类型引用，不触发 SDK 导入
    from app.gateway.base import BasePlatformAdapter, PlatformConfig


@dataclass(frozen=True)
class GatewayPlatformDef:
    """一个平台适配器的完整声明"""

    platform_id: str
    display_name: str
    adapter_factory: Callable[..., Any]  # (PlatformConfig) -> BasePlatformAdapter
    check_requirements: Callable[[], bool] = lambda: True
    config_builder: Optional[Callable[[], Any]] = None
    config_writer: Optional[Callable[[Any], None]] = None
    build_config_values: Optional[Callable[[Dict[str, Any], Optional[Any]], Any]] = None
    validate_config: Optional[Callable[[Any], Tuple[bool, str]]] = None
    ui_order: int = 100
    icon_hint: str = ""
    source: str = ""

    @property
    def id(self) -> str:
        """runtime loader 覆盖判定用（对齐 ModelAdapter.id 约定）。"""
        return self.platform_id