# -*- coding: utf-8 -*-
"""IM 平台契约层（宿主唯一保留的 gateway 模块）。

本包只放**平台插件与网关实现共用的接口**：

- ``base``：``MessageEvent`` / ``SendResult`` / ``ChatInfo`` /
  ``BasePlatformAdapter`` / ``get_cache_dir`` / ``get_gateway_data_dir``
- ``group_history``：群消息留档（平台插件直接 import）

IM 业务实现（管理器、会话映射、媒体、交互提问、引擎、服务）在
``plugins/system-im``；平台适配器在各自的 ``gateway-*`` 插件。
Gitee OAuth / 文件上传 / 本地 LLM API 服务与 IM 无关，已分别迁到
``app/core/auth``、``app/core/net``、``app/core/local_service``。
"""
from app.gateway.base import (
    BasePlatformAdapter,
    ChatInfo,
    MessageEvent,
    MessageType,
    SendResult,
    get_cache_dir,
    get_gateway_data_dir,
)

__all__ = [
    "MessageType",
    "MessageEvent",
    "SendResult",
    "ChatInfo",
    "BasePlatformAdapter",
    "get_cache_dir",
    "get_gateway_data_dir",
]
