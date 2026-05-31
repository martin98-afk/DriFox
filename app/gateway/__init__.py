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
    Platform,
    MessageType,
    MessageEvent,
    SendResult,
    ChatInfo,
    PlatformConfig,
    BasePlatformAdapter,
)

from app.gateway.manager import (
    PlatformManager,
    create_platform_manager,
    get_platform_manager,
)

from app.gateway.config import (
    get_gateway_config,
)

from app.gateway.session_manager import (
    GatewaySession,
    GatewaySessionManager,
)

from app.gateway.message_handler import MessageHandler

# 本地微服务（原 app.api）
from app.gateway.local_service import (
    LLMAPIService,
    get_llm_api_service,
    ensure_service_running,
    start_llm_api_service,
    stop_llm_api_service,
    is_service_running,
    open_docs,
    APISessionHandler,
    APIHistoryManager,
    StreamContext,
    IsolatedChatContext,
    IsolatedContextRegistry,
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