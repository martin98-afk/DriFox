# app/core/conversation/adapters/gateway.py
from typing import Any, Callable, Dict, List, Optional

from app.core.conversation.adapters.base import BaseConversationAdapter
from app.core.conversation.core import ConversationCore
from app.core.conversation.executor import ConversationExecutor


class GatewayConversationAdapter(BaseConversationAdapter):
    """Gateway 对话适配器 — 直接回调

    回调不通过 Qt 信号，而是直接调用注册的函数。
    适用于 Gateway（无 UI）场景。
    """

    def __init__(self, core: ConversationCore, executor: ConversationExecutor):
        super().__init__(core, executor)
        self._callbacks: Dict[str, Callable] = {}

    def set_callbacks(self, callbacks: Dict[str, Callable]):
        """设置外部回调"""
        self._callbacks = callbacks

    def build_messages(self, session, llm_config, current_agent=None):
        """委托 ContextBudgetAllocator 构建消息"""
        return self._core.context_builder.build_messages(
            session=session,
            llm_config=llm_config,
            current_agent=current_agent,
        )

    def on_content_received(self, piece: str):
        cb = self._callbacks.get("content_received")
        if cb:
            cb(piece)

    def on_finished(self, response: str):
        cb = self._callbacks.get("stream_finished")
        if cb:
            cb(response)

    def on_error(self, error: str):
        cb = self._callbacks.get("error")
        if cb:
            cb(error)
