# -*- coding: utf-8 -*-
"""外发 / 平台能力（host = MainWidget）。"""
from __future__ import annotations

from typing import Any


def _send_to_platform(host: Any):
    """向已连接通讯平台会话发送消息（cron-tasks 等插件的通知通道）。

    走 system-im 插件注册的 GatewayService（WindowServiceRegistry 全局槽），
    宿主不 import 插件内部模块；插件未加载时返回失败结果。
    """

    def send(platform: Any, chat_id: str, content: str, **kwargs):
        from app.core.services.gateway_access import get_gateway_service
        from app.gateway.base import SendResult

        svc = get_gateway_service()
        if svc is None:
            return SendResult(success=False, error="IM 网关插件未加载")
        try:
            return svc.send_to_platform(platform, chat_id, content, **kwargs)
        except Exception as e:  # noqa: BLE001 - 外发失败以结果对象返回
            return SendResult(success=False, error=f"GatewayService 调用失败: {e}")

    return send


def _list_platform_sessions(host: Any):
    def list_sessions():
        from app.core.services.gateway_access import get_gateway_service

        svc = get_gateway_service()
        if svc is None:
            return []
        try:
            return svc.list_platform_sessions()
        except Exception:
            return []

    return list_sessions


def _list_platforms(host: Any):
    def list_all():
        from app.core.services.gateway_access import get_gateway_service

        svc = get_gateway_service()
        if svc is None:
            return []
        try:
            return svc.list_platforms()
        except Exception:
            return []

    return list_all


def _platform_control(host: Any):
    """网关控制面：给插件（设置卡/市场/自动化）启停服务与单个平台。

    统一返回 ``{"ok": bool, ...}``，失败带 ``error``，不抛异常，
    让调用方（工具/卡片）自行决定提示方式。
    """

    def control(action: str, platform_id: str = "", enabled: bool = True) -> dict:
        from app.core.services.gateway_access import get_gateway_service

        svc = get_gateway_service()
        if svc is None:
            return {"ok": False, "error": "IM 网关插件未加载"}
        name = str(action or "").lower()
        try:
            if name == "start":
                return {"ok": bool(svc.start())}
            if name == "stop":
                svc.stop()
                return {"ok": True}
            if name == "status":
                return {"ok": True, **(svc.get_status() or {})}
            if name == "set_platform_enabled":
                return {"ok": bool(svc.set_platform_enabled(platform_id, bool(enabled)))}
            if name == "restart_platform":
                svc.restart_platform(platform_id)
                return {"ok": True}
        except Exception as e:  # noqa: BLE001 - 控制面失败以结果返回
            return {"ok": False, "error": str(e)}
        return {"ok": False, "error": f"未知动作: {action}"}

    return control


def register_ui(registry) -> None:
    registry.register("platform.send", _send_to_platform, surface="ui", owner="core:platform")
    registry.register("platform.read", _list_platform_sessions, surface="ui", owner="core:platform")
    registry.register(
        "platform.read", _list_platforms, surface="ui", owner="core:platform", legacy_key="list_platforms"
    )
    registry.register("platform.control", _platform_control, surface="ui", owner="core:platform")
