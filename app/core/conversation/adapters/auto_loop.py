# app/core/conversation/adapters/auto_loop.py
import threading
from typing import Any, Callable, Dict, List, Optional

from app.core.conversation.adapters.base import BaseConversationAdapter
from app.core.conversation.core import ConversationCore
from app.core.conversation.executor import ConversationExecutor


class AutoLoopConversationAdapter(BaseConversationAdapter):
    """AutoLoop 对话适配器 — 线程同步

    AutoLoop 工作在线程中，需要通过 threading.Event 等待 Worker 完成。
    不通过 Qt 信号中转（避免跨线程信号问题），直接等待。
    """

    def __init__(self, core: ConversationCore, executor: ConversationExecutor):
        super().__init__(core, executor)
        self._worker_done_event = threading.Event()
        self._response: str = ""
        self._error: Optional[str] = None

    # AutoLoop 使用自己的 PromptComposer 构建消息，不是 ContextBudgetAllocator
    def build_messages(self, session, llm_config, current_agent=None):
        """AutoLoop 自建消息，不委托 ContextBudgetAllocator"""
        raise NotImplementedError(
            "AutoLoop 使用自己的 PromptComposer 构建消息，不经过此路径"
        )

    def on_content_received(self, piece: str):
        pass  # AutoLoop 不需要实时内容更新

    def on_finished(self, response: str):
        self._response = response
        self._worker_done_event.set()

    def on_error(self, error: str):
        self._error = error
        self._worker_done_event.set()

    def wait_for_completion(self, timeout: float = 30.0) -> bool:
        """等待 Worker 完成，返回是否在超时前完成"""
        return self._worker_done_event.wait(timeout=timeout)

    def get_response(self) -> str:
        """获取完成的响应"""
        return self._response

    def get_error(self) -> Optional[str]:
        """获取错误信息"""
        return self._error

    def reset(self):
        """重置状态（每次迭代前调用）"""
        self._response = ""
        self._error = None
        self._worker_done_event.clear()

    def get_callbacks(self) -> Dict[str, Callable]:
        return {
            "content_received": self.on_content_received,
            "finished": self.on_finished,
            "error": self.on_error,
        }
