# app/core/conversation/adapters/base.py
from __future__ import annotations
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from app.core.conversation.executor import ConversationExecutor

from app.core.conversation.core import ConversationCore


class BaseConversationAdapter:
    """消费者适配器基类

    不同消费者（UI/Gateway/AutoLoop）只需实现：
    - build_messages() — 如何构建 LLM 消息上下文
    - on_content_received() / on_finished() / on_error() — 如何处理回放

    get_callbacks() 返回的回调字典会被 ConversationExecutor 连接到 Worker 信号。
    """

    def __init__(self, core: ConversationCore, executor: ConversationExecutor):
        self._core = core
        self._executor = executor

    def build_messages(
        self, session, llm_config: Dict, current_agent: Optional[str] = None
    ) -> List[Dict]:
        """构建发送给 LLM 的消息列表"""
        raise NotImplementedError

    def on_content_received(self, piece: str):
        """处理 LLM 返回的内容片段"""
        raise NotImplementedError

    def on_finished(self, response: str):
        """处理 LLM 回复完成"""
        raise NotImplementedError

    def on_error(self, error: str):
        """处理错误"""
        raise NotImplementedError

    def get_callbacks(self) -> Dict[str, Callable]:
        """返回回调字典（供 ConversationExecutor 连接 Worker 信号）"""
        return {
            "content_received": self.on_content_received,
            "finished": self.on_finished,
            "error": self.on_error,
        }
