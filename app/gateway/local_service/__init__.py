# -*- coding: utf-8 -*-
"""
Gateway 本地微服务模块 - 提供 HTTP API 接口

复用 UI 对话逻辑的远程 API 接口，支持：
- FastAPI HTTP 服务
- SSE 流式对话
- 完全隔离的会话上下文
- 并发请求支持
"""

from app.gateway.local_service.api_server import (
    LLMAPIService,
    get_llm_api_service,
    ensure_service_running,
    start_llm_api_service,
    stop_llm_api_service,
    is_service_running,
    open_docs,
)

from app.gateway.local_service.session_handler import (
    APISessionHandler,
    APIHistoryManager,
    StreamContext,
)

from app.gateway.local_service.isolated_context import (
    IsolatedChatContext,
    IsolatedContextRegistry,
)

__all__ = [
    # API Server
    "LLMAPIService",
    "get_llm_api_service",
    "ensure_service_running",
    "start_llm_api_service",
    "stop_llm_api_service",
    "is_service_running",
    "open_docs",
    # Session Handler
    "APISessionHandler",
    "APIHistoryManager",
    "StreamContext",
    # Isolated Context
    "IsolatedChatContext",
    "IsolatedContextRegistry",
]
