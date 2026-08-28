# app/core/conversation/adapters/ui.py
from typing import Callable, Dict, List

from PySide6.QtCore import QObject, Signal

from app.core.conversation.adapters.base import BaseConversationAdapter
from app.core.conversation.core import ConversationCore
from app.core.conversation.executor import ConversationExecutor


class UIConversationAdapter(QObject):
    """UI 对话适配器 — Qt 信号转发

    使用组合（而非多继承）持有 BaseConversationAdapter，
    避免 PySide6 的 QObject 与普通 Python 类的 MRO 冲突。
    """

    # Qt 信号
    content_received = Signal(str)
    reasoning_content_received = Signal(str)
    thinking_started = Signal()
    tool_call_started = Signal(str, str, dict, str)  # id, name, args, round
    tool_args_updated = Signal(str, str, dict)
    tool_result_received = Signal(str, str, dict, object)
    question_asked = Signal(str, list, object)  # id, questions(list), extra(dict)
    permission_approval_requested = Signal(str, str, dict)
    stream_started = Signal()
    stream_finished = Signal(str)
    messages_updated = Signal(list)
    error_occurred = Signal(str)
    retry_status = Signal(str, int, int, float)
    retry_resolved = Signal()
    context_updated = Signal(int, int, bool)  # token_count, limit, from_api

    def __init__(self, core: ConversationCore, executor: ConversationExecutor):
        super().__init__()
        self._core = core
        self._executor = executor
        # 组合 BaseConversationAdapter（用于 get_callbacks）
        self._adapter = BaseConversationAdapter(core, executor)

    def build_messages(self, session, llm_config, current_agent=None):
        """委托 ContextBudgetAllocator 构建消息"""
        return self._core.context_builder.build_messages(
            session=session,
            llm_config=llm_config,
            current_agent=current_agent,
        )

    def on_content_received(self, piece: str):
        self.content_received.emit(piece)

    def on_reasoning_content_received(self, piece: str):
        self.reasoning_content_received.emit(piece)

    def on_thinking_started(self):
        self.thinking_started.emit()

    def on_tool_call_started(self, tool_call_id: str, tool_name: str, arguments: dict, round_id: str):
        self.tool_call_started.emit(tool_call_id, tool_name, arguments, round_id)

    def on_tool_args_updated(self, tool_call_id: str, tool_name: str, partial_args: dict):
        self.tool_args_updated.emit(tool_call_id, tool_name, partial_args)

    def on_tool_result_received(self, tool_call_id: str, tool_name: str, arguments: dict, result):
        self.tool_result_received.emit(tool_call_id, tool_name, arguments, result)

    def on_question_asked(self, tool_call_id: str, questions: list, extra: dict = None):
        self.question_asked.emit(tool_call_id, questions, extra)

    def on_permission_approval_requested(self, tool_call_id: str, tool_name: str, arguments: dict):
        self.permission_approval_requested.emit(tool_call_id, tool_name, arguments)

    def on_finished(self, response: str):
        self.stream_finished.emit(response)

    def on_messages_updated(self, messages: List[Dict]):
        # 注意：不在此处调用 consolidate_messages，session.set_messages 内部已做一次 consolidation。
        # 避免主线程上两次全量遍历的冗余开销。
        self.messages_updated.emit(messages or [])

    def on_error(self, error: str):
        self.error_occurred.emit(error)

    def on_retry_status(self, error_type: str, attempt: int, max_retries: int, wait_time: float):
        self.retry_status.emit(error_type, attempt, max_retries, wait_time)

    def on_retry_resolved(self):
        self.retry_resolved.emit()

    def on_stream_started(self):
        self.stream_started.emit()

    def on_context_updated(self, token_count: int, limit: int, from_api: bool = False):
        self.context_updated.emit(token_count, limit, from_api)

    def get_callbacks(self) -> Dict[str, Callable]:
        return {
            "content_received": self.on_content_received,
            "reasoning_content_received": self.on_reasoning_content_received,
            "thinking_started": self.on_thinking_started,
            "tool_call_started": self.on_tool_call_started,
            "tool_args_updated": self.on_tool_args_updated,
            "tool_result_received": self.on_tool_result_received,
            "question_asked": self.on_question_asked,
            "permission_approval_requested": self.on_permission_approval_requested,
            "finished": self.on_finished,
            "messages_updated": self.on_messages_updated,
            "error": self.on_error,
            "retry_status": self.on_retry_status,
            "retry_resolved": self.on_retry_resolved,
            "stream_started": self.on_stream_started,
            "context_updated": self.on_context_updated,
        }
