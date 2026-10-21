# -*- coding: utf-8 -*-
"""外发 / 平台能力（host = MainWidget）。"""

from __future__ import annotations

from typing import Any


def _send_to_platform(host: Any):
    """向已连接通讯平台会话发送消息（cron-tasks 等插件的通知通道）。

    走 GatewayService 应用级单例，不依赖插件侧 import app.gateway
    （其 _manager_instance 由 GatewayService 持有，插件拿不到）。
    """

    def send(platform: Any, chat_id: str, content: str, **kwargs):
        from app.core.services.gateway_service import GatewayService
        from app.gateway.base import SendResult

        try:
            svc = GatewayService.get_instance()
        except Exception as e:
            return SendResult(success=False, error=f"GatewayService 不可用: {e}")
        return svc.send_to_platform(platform, chat_id, content, **kwargs)

    return send


def _list_platform_sessions(host: Any):
    def list_sessions():
        from app.core.services.gateway_service import GatewayService

        try:
            return GatewayService.get_instance().list_platform_sessions()
        except Exception:
            return []

    return list_sessions


def _list_platforms(host: Any):
    def list_all():
        from app.core.services.gateway_service import GatewayService

        try:
            return GatewayService.get_instance().list_platforms()
        except Exception:
            return []

    return list_all


def register_ui(registry) -> None:
    registry.register("platform.send", _send_to_platform, surface="ui", owner="core:platform")
    registry.register("platform.read", _list_platform_sessions, surface="ui", owner="core:platform")
    registry.register(
        "platform.read", _list_platforms, surface="ui", owner="core:platform", legacy_key="list_platforms"
    )
