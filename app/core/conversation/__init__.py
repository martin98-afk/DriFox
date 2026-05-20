# app/core/conversation/__init__.py
from app.core.conversation.config import (
    PermissionStrategy,
    ConversationConfig,
    filter_interactive_tools,
    INTERACTIVE_ONLY_TOOLS,
    PermissionCache,  # 新增：权限缓存（原 permission_cache.py）
)
from app.core.conversation.core import ConversationCore
from app.core.conversation.executor import ConversationExecutor
from app.core.conversation.adapters import (
    BaseConversationAdapter,
    UIConversationAdapter,
    GatewayConversationAdapter,
    AutoLoopConversationAdapter,
)

__all__ = [
    "PermissionStrategy",
    "ConversationConfig",
    "filter_interactive_tools",
    "INTERACTIVE_ONLY_TOOLS",
    "PermissionCache",
    "ConversationCore",
    "ConversationExecutor",
    "BaseConversationAdapter",
    "UIConversationAdapter",
    "GatewayConversationAdapter",
    "AutoLoopConversationAdapter",
]
