# app/core/conversation/adapters/__init__.py
# 延迟导入（避免循环依赖：adapters.ui → executor → workers → auto_loop_worker → adapters）


def __getattr__(name):
    if name == "BaseConversationAdapter":
        from app.core.conversation.adapters.base import BaseConversationAdapter
        return BaseConversationAdapter
    if name == "UIConversationAdapter":
        from app.core.conversation.adapters.ui import UIConversationAdapter
        return UIConversationAdapter
    if name == "GatewayConversationAdapter":
        from app.core.conversation.adapters.gateway import GatewayConversationAdapter
        return GatewayConversationAdapter
    if name == "AutoLoopConversationAdapter":
        from app.core.conversation.adapters.auto_loop import AutoLoopConversationAdapter
        return AutoLoopConversationAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BaseConversationAdapter",
    "UIConversationAdapter",
    "GatewayConversationAdapter",
    "AutoLoopConversationAdapter",
]
