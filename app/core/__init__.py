# -*- coding: utf-8 -*-
"""
LLM Chatter 核心模块
提供聊天引擎、工具执行器、记忆管理等核心功能

注意：通过 __getattr__ 实现懒加载，避免导入 app.core 时触发全量模块加载。
实际子模块在首次访问时才导入（PEP 562）。
"""

import typing as _typing

# ─── 懒加载映射表：{属性名 -> (模块路径, 属性名)} ───────────────────
# 格式：name: (module_path, attr_name)
# 如果 attr_name 为 None 则表示直接返回整个模块
_LAZY_IMPORTS: dict[str, tuple[str, str | None]] = {
    # Backend
    "ChatBackend":             ("app.core.conversation.backend", "ChatBackend"),
    # 引擎与执行器
    "ChatEngine":              ("app.core.engines.ui", "ChatEngine"),
    "ToolExecutor":            ("app.core.tools.tool_executor", "ToolExecutor"),
    "MemoryManagerCore":       ("app.core.conversation.memory_manager", "MemoryManagerCore"),
    # Agent 系统
    "Agent":                   ("app.core.conversation.agent", "Agent"),
    "AgentManager":            ("app.core.conversation.agent", "AgentManager"),
    "create_agent_manager":    ("app.core.conversation.agent", "create_agent_manager"),
    # Worker
    "OpenAIChatWorker":        ("app.core.workers", "OpenAIChatWorker"),
    "SubAgentExecutor":        ("app.core.workers", "SubAgentExecutor"),
    "SubAgentManager":         ("app.core.workers", "SubAgentManager"),
    "TopicSummaryTask":        ("app.core.workers", "TopicSummaryTask"),
    # Store
    "SessionStore":            ("app.core.store", "SessionStore"),
    "SubAgentLogRepository":   ("app.core.store", "SubAgentLogRepository"),
    # 消息处理
    "consolidate_messages":    ("app.core.conversation.message_content", "consolidate_messages"),
    "content_to_text":         ("app.core.conversation.message_content", "content_to_text"),
    "content_to_markdown":     ("app.core.conversation.message_content", "content_to_markdown"),
    "group_messages_for_display": ("app.core.conversation.message_content", "group_messages_for_display"),
    "to_api_message":          ("app.core.conversation.message_content", "to_api_message"),
    "messages_to_api":         ("app.core.conversation.message_content", "messages_to_api"),
    "append_text_block":       ("app.core.conversation.message_content", "append_text_block"),
    "ensure_content_blocks":   ("app.core.conversation.message_content", "ensure_content_blocks"),
    "make_tool_result_block":  ("app.core.conversation.message_content", "make_tool_result_block"),
    "get_user_round_ranges":   ("app.core.conversation.message_content", "get_user_round_ranges"),
    # 重试
    "create_api_call_with_retry": ("app.core.workers.error_handler", "create_api_call_with_retry"),
    "retry_on_api_error":      ("app.core.workers.error_handler", "retry_on_api_error"),
    "SmartRetryHelper":        ("app.core.workers.error_handler", "SmartRetryHelper"),
    "RetryConfig":             ("app.core.workers.error_handler", "RetryConfig"),
    "RetryResult":             ("app.core.workers.error_handler", "RetryResult"),
    "ErrorClassifier":         ("app.core.workers.error_handler", "ErrorClassifier"),
    "ClassifiedError":         ("app.core.workers.error_handler", "ClassifiedError"),
    "FailoverReason":          ("app.core.workers.error_handler", "FailoverReason"),
    # 子模块（直接返回 module 对象）
    "error_handler":           ("app.core.workers.error_handler", None),
    # Token
    "estimate_tokens":         ("app.core.infra.token_estimator", "estimate_tokens"),
    "count_messages_tokens":   ("app.core.infra.token_estimator", "count_messages_tokens"),
    "TokenCounter":            ("app.core.infra.token_estimator", "TokenCounter"),
    # 会话
    "ChatSession":             ("app.core.conversation.chat_session", "ChatSession"),
    "SessionManager":          ("app.core.conversation.chat_session", "SessionManager"),
}


def __getattr__(name: str) -> _typing.Any:
    """PEP 562 懒加载：访问时才导入对应子模块"""
    if name not in _LAZY_IMPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_path, attr_name = _LAZY_IMPORTS[name]
    import importlib as _importlib

    module = _importlib.import_module(module_path)

    if attr_name is None:
        # 直接返回模块本身（如 error_handler）
        value = module
    else:
        value = getattr(module, attr_name)

    # 缓存到模块命名空间，下次访问直接走 sys.modules 缓存
    globals()[name] = value
    return value

__all__ = [
    # Backend
    "ChatBackend",
    # 引擎与执行器
    "ChatEngine",
    "ToolExecutor",
    "MemoryManagerCore",
    # Agent 系统
    "Agent",
    "AgentManager",
    "create_agent_manager",
    # Worker
    "OpenAIChatWorker",
    "SubAgentExecutor",
    "SubAgentManager",
    "TopicSummaryTask",
    # Store
    "SessionStore",
    "SubAgentLogRepository",
    # 消息处理
    "consolidate_messages",
    "content_to_text",
    "content_to_markdown",
    "group_messages_for_display",
    "to_api_message",
    "messages_to_api",
    "append_text_block",
    "ensure_content_blocks",
    "make_tool_result_block",
    "get_user_round_ranges",
    # 重试
    "create_api_call_with_retry",
    "retry_on_api_error",
    "error_handler",
    # Smart Retry
    "SmartRetryHelper",
    "RetryConfig",
    "RetryResult",
    "ErrorClassifier",
    "ClassifiedError",
    "FailoverReason",
    # Token
    "estimate_tokens",
    "count_messages_tokens",
    "TokenCounter",
    # 会话
    "ChatSession",
    "SessionManager",
]
