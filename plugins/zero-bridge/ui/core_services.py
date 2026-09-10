# -*- coding: utf-8 -*-
"""系统单例 → zero ctx 注册表（第 1 步：主系统零件插入信息池）。

把 DriFox 的应用级单例挂到 zero 根上下文，插件从此
``@inject(required=["backend"])`` / ``ctx.require("event_bus")`` 直取，
不需要知道单例在哪个模块、怎么 get_instance。

设计约束：
- **延迟导入**：getter 函数体内才 import app.*（本模块 import 零副作用，
  插件加载早于主窗口就绪也不炸）
- **行为不变**：只做「登记可见」，单例的构造、生命周期、调用方式一律不动
- **键名即契约**：注入服务名保持短名（backend / event_bus / ui_registry…），
  一旦插件引用即成为稳定契约，改名 = 破坏性变更
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Tuple

from zero import Context

logger = logging.getLogger(__name__)

__all__ = ["CoreService", "CORE_SERVICES", "register_core_services"]


@dataclass(frozen=True)
class CoreService:
    """一个待入池的系统单例。"""

    name: str  # ctx 服务名（注入契约）
    getter: Callable[[], Any]  # 延迟取单例（函数内 import app.*）
    weak: bool = False  # 单例与进程同寿，默认强引用


def _plugin_host() -> Any:
    from app.core.plugin_host_service import PluginHostService

    return PluginHostService.get_instance()


def _agent_manager() -> Any:
    from app.core.agent import AgentManager

    am = AgentManager.get_instance(None, None)  # type: ignore[arg-type]
    return am


def _event_bus() -> Any:
    from app.core.ui_event_bus import UIEventBus

    return UIEventBus.get_instance()


def _settings() -> Any:
    from app.utils.config import Settings

    return Settings()


def _hook_manager() -> Any:
    """host 级 HookManager（与 AgentManager 持有同一实例）。"""
    from app.core.plugin_host_service import PluginHostService

    return PluginHostService.get_instance()._host_hook_manager


def _make_registry_getter(module: str, cls: str) -> Callable[[], Any]:
    def _get() -> Any:
        import importlib

        mod = importlib.import_module(module)
        return getattr(mod, cls).get_instance()

    return _get


# ── 入池清单（增删即改此处；键名成为插件注入契约）──────────────────
CORE_SERVICES: Tuple[CoreService, ...] = (
    CoreService("plugin_host", _plugin_host),
    CoreService("agents", _agent_manager),
    CoreService("hook_manager", _hook_manager),
    CoreService("event_bus", _event_bus),
    CoreService("settings", _settings),
    CoreService(
        "storage_registry", _make_registry_getter("app.plugins.registries.storage_registry", "StorageRegistry")
    ),
    CoreService(
        "provider_registry", _make_registry_getter("app.plugins.registries.provider_registry", "ProviderRegistry")
    ),
    CoreService(
        "model_adapter_registry",
        _make_registry_getter("app.plugins.registries.model_adapter_registry", "ModelAdapterRegistry"),
    ),
    CoreService(
        "loop_policy_registry",
        _make_registry_getter("app.plugins.registries.loop_policy_registry", "LoopPolicyRegistry"),
    ),
    CoreService(
        "hook_policy_registry",
        _make_registry_getter("app.plugins.registries.hook_policy_registry", "HookPolicyRegistry"),
    ),
    CoreService(
        "serializer_registry", _make_registry_getter("app.plugins.registries.serializer_registry", "SerializerRegistry")
    ),
    CoreService(
        "gateway_registry",
        _make_registry_getter("app.plugins.registries.gateway_platform_registry", "GatewayPlatformRegistry"),
    ),
    CoreService("engine_registry", _make_registry_getter("app.plugins.registries.engine_registry", "EngineRegistry")),
    CoreService("ui_registry", _make_registry_getter("app.plugins.registries.ui_plugin_registry", "UIPluginRegistry")),
    CoreService(
        "plugin_config_registry",
        _make_registry_getter("app.plugins.registries.plugin_config_registry", "PluginConfigRegistry"),
    ),
)


def register_core_services(root: Context) -> List[str]:
    """把系统单例注册到根上下文（register_ui 时机调用，延迟 getter 此刻才执行）。"""
    registered: List[str] = []
    for service in CORE_SERVICES:
        try:
            instance = service.getter()
        except Exception as e:
            logger.warning(f"[zero-bridge] 系统单例 {service.name!r} 入池失败（跳过）: {e!r}")
            continue
        root.set(service.name, instance, weak=service.weak)
        registered.append(service.name)
    logger.info(f"[zero-bridge] 系统零件已入池: {registered}")
    return registered


def core_service_names() -> List[str]:
    """清单里的全部服务名（诊断用，不触发 getter）。"""
    return [s.name for s in CORE_SERVICES]
