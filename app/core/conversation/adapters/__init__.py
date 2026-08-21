# app/core/conversation/adapters/__init__.py
# 延迟导入（避免循环依赖：adapters.ui → executor → workers → adapters）
# 注：AutoLoopConversationAdapter 已随 autoloop 插件化迁移至 plugins/autoloop/autoloop_core/adapter.py


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
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BaseConversationAdapter",
    "UIConversationAdapter",
    "GatewayConversationAdapter",
]
