# -*- coding: utf-8 -*-
"""system-im UI 组件入口。

两件事：
1. **服务注册**：把插件持有的 ``GatewayService`` 写入宿主
   ``WindowServiceRegistry`` 全局槽（key = ``im_gateway``），宿主 UI、
   ``platform.*`` 能力提供方与 plugin-marketplace 按 key 取用，不再
   import ``app.gateway.manager``。
2. **设置卡**：注册「设置 → 插件」页的 IM 网关卡（服务总开关 + 平台在线
   列表 + 每平台启停/重连）。

模块级 ``_service`` 必须活到插件卸载：``unload_ui`` 里停平台连接并摘除服务。
"""
from __future__ import annotations

from typing import Any, Optional

from loguru import logger

from .core_loader import load_core, load_root_module
from .settings_card import SERVICE_KEY, ImGatewaySettingsCard

_PLUGIN_NAME = "system-im"
_PREF_KEY = "service_enabled"

_service: Optional[Any] = None


def _service_enabled_pref() -> bool:
    """总开关的持久化状态（缺省开启，与迁移前的启动即连行为一致）。"""
    try:
        from app.plugins.managers.plugin_config_store import PluginConfigStore

        value = PluginConfigStore().get(_PLUGIN_NAME, _PREF_KEY)
        return True if value is None else bool(value)
    except Exception as e:  # noqa: BLE001 - 配置不可读按默认值
        logger.debug(f"[{_PLUGIN_NAME}] 服务开关读取失败: {e}")
        return True


def _ensure_service() -> Optional[Any]:
    """构造（或复用）GatewayService（进程级单例）。"""
    global _service
    if _service is not None:
        return _service
    try:
        load_core()
        service_mod = load_root_module("service")
        _service = service_mod.GatewayService.get_instance()
    except Exception as e:  # noqa: BLE001 - 服务起不来不拖垮宿主设置页
        logger.error(f"[{_PLUGIN_NAME}] GatewayService 初始化失败: {e}", exc_info=True)
        return None
    return _service


def _register_service(service: Any) -> None:
    """把服务挂到窗口服务全局槽（幂等：热重载会清空本插件注册项）。"""
    try:
        from app.plugins.registries.window_services import get_window_services

        get_window_services().register(SERVICE_KEY, service, plugin_name=_PLUGIN_NAME)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[{_PLUGIN_NAME}] 服务注册失败: {e}")


def register_ui(registry) -> None:
    """全局网关服务 + 设置卡。"""
    service = _ensure_service()
    if service is not None:
        _register_service(service)
    if service is not None and _service_enabled_pref():
        try:
            service.start()
        except Exception as e:  # noqa: BLE001 - 启动失败留给设置卡手动重试
            logger.warning(f"[{_PLUGIN_NAME}] 网关服务启动失败: {e}")

    try:
        registry.register_settings_card(
            _PLUGIN_NAME,
            "im_gateway",
            "即时通讯网关",
            ImGatewaySettingsCard,
            section="plugins",
            priority=20,
            metadata={"description": "IM 服务开关与平台在线状态"},
        )
    except Exception as e:  # noqa: BLE001 - 注册失败不拖垮宿主设置页
        logger.warning(f"[{_PLUGIN_NAME}] 设置卡注册失败: {e}")


def unload_ui(registry=None) -> None:
    """插件卸载/热重载：停平台连接并摘除全局服务（宿主传 registry，本插件不用）。"""
    global _service
    service, _service = _service, None
    if service is not None:
        try:
            service.stop()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{_PLUGIN_NAME}] 停止网关服务失败: {e}")
    try:
        from app.plugins.registries.window_services import get_window_services

        get_window_services().unregister_plugin(_PLUGIN_NAME)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[{_PLUGIN_NAME}] 服务摘除失败: {e}")
