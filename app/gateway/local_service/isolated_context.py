# -*- coding: utf-8 -*-
"""
Gateway 本地微服务 - 隔离上下文

为 API 调用创建完全独立的环境，与 UI 完全隔离。
"""

import uuid
import threading
from typing import Optional, Dict, List, Callable, Any
from loguru import logger

from app.core import SessionManager, ChatSession


class IsolatedChatContext:
    """API 隔离上下文 - 封装完全独立的组件实例"""

    def __init__(self, context_id: str, main_widget, target_session=None, model_config=None):
        self.context_id = context_id
        self._main_widget = main_widget
        self._model_config = model_config
        self._session_manager = self._create_session_manager(target_session)
        self._tool_executor = self._create_tool_executor()
        self._agent_manager = self._create_agent_manager()
        self._context_provider = None
        self._lock = threading.Lock()

        if target_session:
            self._session_manager.set_current_session(target_session)

    def _create_session_manager(self, target_session):
        manager = SessionManager()
        if target_session:
            session_copy = ChatSession.from_dict({
                "session_id": target_session.session_id,
                "name": target_session.name,
                "messages": list(target_session.messages) if target_session.messages else [],
                "topic_summary": target_session.topic_summary,
                "created_at": target_session.created_at,
                "last_updated": target_session.last_updated,
            })
            manager.sessions.append(session_copy)
            manager.current_index = 0
        else:
            manager.create_new_session()
        return manager

    def _create_tool_executor(self):
        from app.core.tool_executor import ToolExecutor
        ui_tool_executor = getattr(self._main_widget, '_tool_executor', None)
        homepage = getattr(self._main_widget, 'homepage', None)
        executor = ToolExecutor(homepage=homepage)
        if ui_tool_executor and ui_tool_executor._builtin_tools:
            executor._builtin_tools = ui_tool_executor._builtin_tools
        current_session = self._session_manager.get_current_session()
        session_id = current_session.session_id if current_session else str(uuid.uuid4())
        executor.set_session_context(session_id, call_id=None)
        executor.set_session_messages_getter(self._get_session_messages_for_tools)
        executor.set_agent_manager(self._agent_manager)
        return executor

    def _get_session_messages_for_tools(self) -> List[Dict[str, Any]]:
        session = self._session_manager.get_current_session()
        if not session:
            return []
        return list(session.messages or [])

    def _create_agent_manager(self):
        from app.core.agent import AgentManager
        return AgentManager.get_instance()

    def _get_model_config(self) -> Dict[str, Any]:
        if self._model_config:
            return self._model_config
        if hasattr(self._main_widget, '_get_current_model_config'):
            return self._main_widget._get_current_model_config()
        return {}

    def _get_context_provider(self):
        return None

    def set_call_id(self, call_id: str) -> None:
        with self._lock:
            if self._tool_executor:
                self._tool_executor.set_call_id(call_id)

    def update_session_context(self) -> None:
        with self._lock:
            current_session = self._session_manager.get_current_session()
            if current_session and self._tool_executor:
                self._tool_executor.set_session_context(current_session.session_id, call_id=None)

    def create_chat_engine(self, worker_callbacks=None, api_mode=True):
        from app.core.engines.ui import ChatEngine
        engine = ChatEngine(
            session_manager=self._session_manager,
            get_model_config=self._get_model_config,
            get_context_provider=self._get_context_provider,
            tool_executor=self._tool_executor,
            agent_manager=self._agent_manager,
            get_chat_cards=None,
            get_memory_context=getattr(self._main_widget, '_build_memory_context_for_engine', None),
            worker_callbacks=worker_callbacks,
            api_mode=api_mode,
        )
        return engine

    def get_current_session(self):
        return self._session_manager.get_current_session()

    def add_message(self, role: str, content: str) -> None:
        session = self._session_manager.get_current_session()
        if session:
            if role == "user":
                session.add_user_message(content)
            else:
                session.add_assistant_message(content)

    def get_messages(self) -> List[Dict[str, Any]]:
        session = self._session_manager.get_current_session()
        if session:
            return list(session.messages or [])
        return []

    def cleanup(self) -> None:
        with self._lock:
            if self._tool_executor:
                self._tool_executor._session_id = None
                self._tool_executor._call_id = None
                if self._tool_executor._builtin_tools:
                    self._tool_executor._builtin_tools.todo_clear()
            logger.debug(f"[GatewayLocal] 上下文已清理: {self.context_id}")


class IsolatedContextRegistry:
    """隔离上下文注册表 - 单例"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._contexts: Dict[str, IsolatedChatContext] = {}
                    cls._instance._context_lock = threading.Lock()
        return cls._instance

    @classmethod
    def get_instance(cls) -> "IsolatedContextRegistry":
        return cls()

    def create_context(self, context_id, main_widget, target_session=None, model_config=None):
        with self._context_lock:
            if context_id in self._contexts:
                self._contexts[context_id].cleanup()
            ctx = IsolatedChatContext(
                context_id=context_id,
                main_widget=main_widget,
                target_session=target_session,
                model_config=model_config,
            )
            self._contexts[context_id] = ctx
            logger.debug(f"[ContextRegistry] 创建新上下文: {context_id}")
            return ctx

    def get_context(self, context_id: str) -> Optional[IsolatedChatContext]:
        with self._context_lock:
            return self._contexts.get(context_id)

    def remove_context(self, context_id: str) -> None:
        with self._context_lock:
            if context_id in self._contexts:
                self._contexts[context_id].cleanup()
                del self._contexts[context_id]
                logger.debug(f"[ContextRegistry] 移除上下文: {context_id}")

    def get_active_count(self) -> int:
        with self._context_lock:
            return len(self._contexts)

    def cleanup_all(self) -> None:
        with self._context_lock:
            for ctx in self._contexts.values():
                ctx.cleanup()
            self._contexts.clear()
            logger.info("[ContextRegistry] 所有上下文已清理")
