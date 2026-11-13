# -*- coding: utf-8 -*-
"""IM 网关服务的宿主侧访问器。

网关实现（PlatformManager / GatewayEngine / GatewayService）已迁入
``plugins/system-im`` 插件。插件在 ``register_ui`` 时把 ``GatewayService``
写入宿主 ``WindowServiceRegistry`` 的全局槽（key = ``im_gateway``）；
本模块是宿主唯一的取用入口，取不到即表示插件未加载/已禁用，调用方按
「网关不可用」降级，不 import 插件内部模块。
"""
from __future__ import annotations

from typing import Any, Optional

GATEWAY_SERVICE_KEY = "im_gateway"


def get_gateway_service() -> Optional[Any]:
    """取插件持有的 GatewayService（未加载返回 None）。"""
    try:
        from app.plugins.registries.window_services import get_window_services

        return get_window_services().get(GATEWAY_SERVICE_KEY)
    except Exception:  # noqa: BLE001 - 服务表未就绪按未加载处理
        return None


def get_platform_manager() -> Optional[Any]:
    """取插件持有的 PlatformManager（服务未启动返回 None）。"""
    svc = get_gateway_service()
    if svc is None:
        return None
    try:
        return svc.get_manager()
    except Exception:  # noqa: BLE001
        return None


def shutdown_gateway_service() -> None:
    """退出清理：停连接 + 关闭事件循环线程 + 释放引擎（幂等）。"""
    svc = get_gateway_service()
    if svc is None:
        return
    try:
        svc.shutdown()
    except Exception:  # noqa: BLE001 - 退出路径不抛
        pass
