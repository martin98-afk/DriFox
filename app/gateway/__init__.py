# -*- coding: utf-8 -*-
"""
Gateway 模块 - 企业微信/钉钉等通讯平台接入

DriFox Gateway 使 AI 能够通过企业微信、钉钉等平台与用户交互。

基本用法:

    from app.gateway import PlatformManager, get_gateway_config, Platform

    # 创建管理器
    config = get_gateway_config()
    manager = PlatformManager(config)

    # 设置回调
    async def process_message(session_id, text, platform, ...):
        # 调用 AI 处理
        return await ai.process(text)

    async def send_message(platform, chat_id, content, ...):
        adapter = manager.get_adapter(platform)
        return await adapter.send(chat_id, content)

    manager.set_process_callback(process_message, send_message)

    # 启动
    await manager.start_all()
"""

from app.gateway.base import (
    BasePlatformAdapter,
    ChatInfo,
    MessageEvent,
    MessageType,
    Platform,
    PlatformConfig,
    SendResult,
)
import typing as _typing

from app.gateway.config import (
    get_gateway_config,
)

# [PERF] 本地微服务（原 app.api）改为 PEP 562 按需导入。
#
# local_service/api_server 会拉起 fastapi 全家桶（实测 ~150ms），而多数
# 调用方只需要 gateway 的轻量部分——例如 `import app.gateway.auth`
# （Gitee OAuth）也会先执行本包的 __init__，白白付掉这份导入成本
# （实测：GiteeCard 构造 → _auto_enable_sync → app.gateway.auth → fastapi）。
# 改为 __getattr__ 懒加载后行为完全等价，只有真正用到 LLM API 服务时才导入。
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "APIHistoryManager": ("app.gateway.local_service", "APIHistoryManager"),
    "APISessionHandler": ("app.gateway.local_service", "APISessionHandler"),
    "IsolatedChatContext": ("app.gateway.local_service", "IsolatedChatContext"),
    "IsolatedContextRegistry": ("app.gateway.local_service", "IsolatedContextRegistry"),
    "LLMAPIService": ("app.gateway.local_service", "LLMAPIService"),
    "StreamContext": ("app.gateway.local_service", "StreamContext"),
    "ensure_service_running": ("app.gateway.local_service", "ensure_service_running"),
    "get_llm_api_service": ("app.gateway.local_service", "get_llm_api_service"),
    "is_service_running": ("app.gateway.local_service", "is_service_running"),
    "open_docs": ("app.gateway.local_service", "open_docs"),
    "start_llm_api_service": ("app.gateway.local_service", "start_llm_api_service"),
    "stop_llm_api_service": ("app.gateway.local_service", "stop_llm_api_service"),
}


def __getattr__(name: str) -> _typing.Any:
    if name not in _LAZY_IMPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_path, attr = _LAZY_IMPORTS[name]
    import importlib as _importlib

    value = getattr(_importlib.import_module(module_path), attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(list(globals()) + list(_LAZY_IMPORTS)))


from app.gateway.manager import (
    PlatformManager,
    create_platform_manager,
    get_platform_manager,
)
from app.gateway.message_handler import MessageHandler
from app.gateway.session_manager import (
    GatewaySession,
    GatewaySessionManager,
)

__all__ = [
    # 基础
    "Platform",
    "MessageType",
    "MessageEvent",
    "SendResult",
    "ChatInfo",
    "PlatformConfig",
    "BasePlatformAdapter",
    # 管理器
    "PlatformManager",
    "create_platform_manager",
    "get_platform_manager",
    # 配置
    "get_gateway_config",
    # 会话
    "GatewaySession",
    "GatewaySessionManager",
    # 消息处理
    "MessageHandler",
    # 本地微服务
    "LLMAPIService",
    "get_llm_api_service",
    "ensure_service_running",
    "start_llm_api_service",
    "stop_llm_api_service",
    "is_service_running",
    "open_docs",
    "APISessionHandler",
    "APIHistoryManager",
    "StreamContext",
    "IsolatedChatContext",
    "IsolatedContextRegistry",
]
