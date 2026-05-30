# -*- coding: utf-8 -*-
"""
Gateway 本地微服务 - 会话处理器

完全隔离的 API 调用，支持并发和持久化。
"""

import asyncio
import orjson as json
import threading
import uuid
from typing import Optional, Dict, Any, List, Callable, AsyncGenerator
from loguru import logger

from app.core import ChatSession


class StreamContext:
    """流式请求上下文（线程安全）"""
    
    def __init__(self, stream_id: str):
        self.stream_id = stream_id
        self.engine = None
        self.session_id = ""
        self.context_id = ""
        self.buffer: Dict[str, Any] = {
            "content": "",
            "started": False,
            "finished": False,
        }
        self.sse_queue: asyncio.Queue = asyncio.Queue()
        self._active = True
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._pending_event: Optional[Dict[str, Any]] = None

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def set_active(self, active: bool) -> None:
        with self._lock:
            self._active = active
        if not active:
            self._event.set()

    def wait_for_event(self, timeout: float = 0.5) -> Optional[Dict[str, Any]]:
        """等待事件发生（用于 API 模式，替代 Qt 信号）"""
        self._event.wait(timeout=timeout)
        self._event.clear()
        with self._lock:
            event = self._pending_event
            self._pending_event = None
            return event

    def push_event(self, event_data: Dict[str, Any]) -> None:
        """推送事件"""
        with self._lock:
            self._pending_event = event_data
        self._event.set()

    def append_content(self, piece: str) -> None:
        with self._lock:
            self.buffer["content"] += piece

    def get_content(self) -> str:
        with self._lock:
            return self.buffer.get("content", "")


class APIHistoryManager:
    """API 专用历史管理器 - 固化到 SQLite"""
    
    def __init__(self, ui_history_manager):
        self._ui_history_manager = ui_history_manager
        self._session_store = None
        self._init_sqlite()
        self._api_sessions: List[Dict[str, Any]] = []
        self._load_from_sqlite()
    
    def _init_sqlite(self):
        """初始化 SQLite 存储"""
        try:
            from app.core.store import SessionStore
            self._session_store = SessionStore.get_instance()
            if self._session_store.is_initialized:
                logger.info("[APIHistoryManager] SQLite 存储已启用")
            else:
                logger.warning("[APIHistoryManager] SQLite 初始化失败")
        except Exception as e:
            logger.error(f"[APIHistoryManager] SQLite 初始化异常: {e}")
    
    def _load_from_sqlite(self):
        """从 SQLite 加载会话到内存"""
        if self._session_store and self._session_store.is_initialized:
            try:
                self._api_sessions = self._session_store.get_sessions(limit=100)
                logger.debug(f"[APIHistoryManager] 从 SQLite 加载 {len(self._api_sessions)} 条会话")
            except Exception as e:
                logger.error(f"[APIHistoryManager] 加载失败: {e}")
    
    @property
    def _history_sessions(self) -> List[Dict[str, Any]]:
        return self._api_sessions
    
    @_history_sessions.setter
    def _history_sessions(self, value: List[Dict[str, Any]]):
        self._api_sessions = value
    
    def _persist_session(self, session_record: Dict) -> None:
        if self._session_store and self._session_store.is_initialized:
            self._session_store.save_session(session_record)
    
    def get_history_list(self) -> List[Dict]:
        return sorted(self._api_sessions, key=lambda x: x.get("last_time", ""), reverse=True)
    
    def get_session_by_session_id(self, session_id: str) -> Optional[Dict]:
        for s in self._api_sessions:
            if s.get("session_id") == session_id:
                return s
        if self._session_store and self._session_store.is_initialized:
            return self._session_store.get_session(session_id)
        return None
    
    def get_session_by_index(self, idx: int) -> Optional[List[Dict]]:
        if 0 <= idx < len(self._api_sessions):
            return self._api_sessions[idx].get("messages", [])
        return None
    
    def find_index_by_session_id(self, session_id: str) -> int:
        for i, s in enumerate(self._api_sessions):
            if s.get("session_id") == session_id:
                return i
        return -1
    
    def get_current_title(self, idx: int) -> str:
        if 0 <= idx < len(self._api_sessions):
            return self._api_sessions[idx].get("title", "未命名")
        return "未命名"
    
    def save_session(self, messages: List[Dict], title: str, session_id: str) -> None:
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        session_record = {
            "session_id": session_id,
            "title": title,
            "messages": messages,
            "created_at": now,
            "updated_at": now,
            "compaction_state": {},
            "compaction_cache": {},
            "system_prompt": "",
            "message_count": len(messages),
        }
        
        existing_idx = self.find_index_by_session_id(session_id)
        if existing_idx >= 0:
            existing = self._api_sessions[existing_idx]
            session_record["created_at"] = existing.get("created_at", now)
            self._api_sessions[existing_idx] = session_record
        else:
            self._api_sessions.insert(0, session_record)
        
        self._api_sessions = self._api_sessions[:100]
        self._persist_session(session_record)
        logger.debug(f"[APIHistoryManager] 保存会话: {session_id}, 消息数: {len(messages)}")
    
    def archive_history(self, idx: int) -> bool:
        if 0 <= idx < len(self._api_sessions):
            session = self._api_sessions[idx]
            session_id = session.get("session_id")
            self._api_sessions.pop(idx)
            if self._session_store and self._session_store.is_initialized:
                try:
                    logger.debug(f"[APIHistoryManager] 删除会话: {session_id}")
                except Exception as e:
                    logger.error(f"[APIHistoryManager] 删除失败: {e}")
            return True
        return False


class APISessionHandler:
    """API 会话处理器 - 完全隔离的实现"""
    
    def __init__(self, main_widget):
        self._main_widget = main_widget
        self._lock = threading.Lock()
        self._api_callbacks: Dict[str, Callable] = {}
        self._active_streams: Dict[str, StreamContext] = {}
        from app.gateway.local_service.isolated_context import IsolatedContextRegistry
        self._context_registry = IsolatedContextRegistry.get_instance()
        ui_history_manager = getattr(main_widget, 'history_manager', None)
        self._api_history_manager = APIHistoryManager(ui_history_manager)
    
    @property
    def session_manager(self):
        return self._api_history_manager
    
    @property
    def history_manager(self):
        return self._api_history_manager
    
    @property
    def tool_executor(self):
        raise RuntimeError(
            "APISessionHandler.tool_executor 已废弃，请使用 IsolatedChatContext._tool_executor"
        )
    
    @property
    def agent_manager(self):
        return self._main_widget._agent_manager
    
    def _get_model_config(self) -> Dict[str, Any]:
        return self._main_widget._get_current_model_config()
    
    def _get_context_provider(self):
        return None
    
    def set_api_callback(self, event: str, callback: Callable) -> None:
        self._api_callbacks[event] = callback
    
    def _create_isolated_chat_engine(self, worker_callbacks=None, api_mode=False, target_session=None, context_id=None):
        from app.gateway.local_service.isolated_context import IsolatedChatContext
        
        ctx_id = context_id or str(uuid.uuid4())
        isolated_context = IsolatedChatContext(
            context_id=ctx_id,
            main_widget=self._main_widget,
            target_session=target_session,
            model_config=self._get_model_config(),
        )
        engine = isolated_context.create_chat_engine(
            worker_callbacks=worker_callbacks,
            api_mode=api_mode,
        )
        engine._isolated_context = isolated_context
        self._context_registry._contexts[ctx_id] = isolated_context
        return engine
    
    def _persist_current_session_from_engine(self, engine) -> None:
        try:
            session = engine._session_manager.get_current_session()
            if not session:
                return
            session_id = session.session_id
            messages = []
            for msg in session.messages:
                if isinstance(msg, dict):
                    messages.append(msg)
                elif hasattr(msg, 'role'):
                    messages.append({
                        "role": getattr(msg, 'role', 'user'),
                        "content": getattr(msg, 'content', ''),
                        "timestamp": getattr(msg, 'timestamp', ''),
                    })
            if not messages:
                return
            if self.history_manager:
                self.history_manager.save_session(
                    messages=messages,
                    title=session.topic_summary or session.name or "API 对话",
                    session_id=session_id,
                )
            logger.debug(f"[GatewayLocal] 会话已持久化: {session_id}")
        except Exception as e:
            logger.warning(f"[GatewayLocal] 持久化会话失败: {e}")
    
    def list_sessions(self) -> List[Dict[str, Any]]:
        try:
            if self.history_manager:
                sessions = self.history_manager.get_history_list()
                return [
                    {
                        "id": s.get("session_id", ""),
                        "title": s.get("title", "未命名"),
                        "created_at": s.get("created_at", ""),
                        "updated_at": s.get("last_updated", ""),
                        "message_count": s.get("message_count", 0),
                    }
                    for s in sessions
                ]
            sessions = self.session_manager.list_sessions()
            return [
                {
                    "id": s.id,
                    "title": s.topic_summary or s.name or "未命名",
                    "created_at": s.created_at,
                    "updated_at": s.last_updated,
                    "message_count": len(s.messages),
                }
                for s in sessions
            ]
        except Exception as e:
            logger.error(f"[GatewayLocal] list_sessions 失败: {e}")
            return []
    
    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        try:
            if self.history_manager:
                session_data = self.history_manager.get_session_by_session_id(session_id)
                if session_data:
                    return session_data
            if self.history_manager:
                idx = self.history_manager.find_index_by_session_id(session_id)
                if idx >= 0:
                    session_data = self.history_manager.get_session_by_index(idx)
                    if session_data:
                        return {
                            "id": session_id,
                            "title": self.history_manager.get_current_title(idx),
                            "messages": session_data,
                        }
            return None
        except Exception as e:
            logger.error(f"[GatewayLocal] get_session 失败: {e}")
            return None
    
    def create_session(self, title: str = "") -> Optional[Dict[str, Any]]:
        try:
            session = ChatSession(name=title or "API 对话")
            session_name = title or "API 对话"
            if self.history_manager:
                self.history_manager.save_session(
                    messages=[],
                    title=session_name,
                    session_id=session.session_id,
                )
            logger.debug(f"[GatewayLocal] 创建新会话: {session.session_id}")
            return {
                "id": session.session_id,
                "title": session_name,
                "created_at": session.created_at,
                "updated_at": session.last_updated,
            }
        except Exception as e:
            logger.error(f"[GatewayLocal] create_session 失败: {e}")
            return None
    
    def delete_session(self, session_id: str) -> bool:
        try:
            if self.history_manager:
                idx = self.history_manager.find_index_by_session_id(session_id)
                if idx >= 0:
                    self.history_manager.archive_history(idx)
                    logger.debug(f"[GatewayLocal] 删除会话: {session_id}")
                    return True
            logger.warning(f"[GatewayLocal] 会话不存在: {session_id}")
            return False
        except Exception as e:
            logger.error(f"[GatewayLocal] delete_session 失败: {e}")
            return False
    
    def switch_session(self, session_id: str) -> Optional[ChatSession]:
        try:
            if self.history_manager:
                session_data = self.history_manager.get_session_by_session_id(session_id)
                if session_data:
                    session = ChatSession.from_dict({
                        "session_id": session_data.get("session_id", session_id),
                        "name": session_data.get("title", "未命名"),
                        "messages": session_data.get("messages", []),
                        "topic_summary": session_data.get("title", ""),
                        "created_at": session_data.get("created_at"),
                        "last_updated": session_data.get("last_updated"),
                        "system_prompt": session_data.get("system_prompt", ""),
                    })
                    return session
            logger.warning(f"[GatewayLocal] 会话不存在: {session_id}")
            return None
        except Exception as e:
            logger.error(f"[GatewayLocal] switch_session 失败: {e}")
            return None
    
    def _set_engine_session(self, engine, session: ChatSession) -> None:
        session_copy = ChatSession.from_dict({
            "session_id": session.session_id,
            "name": session.name,
            "messages": list(session.messages),
            "topic_summary": session.topic_summary,
            "created_at": session.created_at,
            "last_updated": session.last_updated,
        })
        engine._session_manager.set_current_session(session_copy)
    
    async def chat_stream(self, session_id: str, message: str, context_params=None) -> AsyncGenerator[str, None]:
        stream_id = str(uuid.uuid4())
        target_session = self.switch_session(session_id)
        if not target_session:
            yield f"data: {json.dumps({'error': f'会话 {session_id} 不存在'})}\n\n"
            return
        
        ctx = StreamContext(stream_id)
        ctx.session_id = session_id
        self._active_streams[stream_id] = ctx
        
        def make_callback(event_name: str):
            def callback(*args, **kwargs):
                self._handle_engine_event(stream_id, event_name, *args, **kwargs)
            return callback
        
        worker_callbacks = {
            "content_received": make_callback("content"),
            "reasoning_content_received": make_callback("reasoning"),
            "tool_call_started": make_callback("tool_call_started"),
            "tool_result_received": make_callback("tool_result"),
            "error_occurred": make_callback("error"),
            "finished_with_content": make_callback("stream_finished"),
            "finished_with_messages": make_callback("messages_updated"),
            "question_asked": make_callback("question"),
            "permission_approval_requested": make_callback("permission"),
        }
        
        engine = self._create_isolated_chat_engine(
            worker_callbacks=worker_callbacks,
            api_mode=True,
            target_session=target_session,
            context_id=stream_id,
        )
        ctx.engine = engine
        ctx.context_id = stream_id
        
        try:
            yield f"data: {json.dumps({'stream_id': stream_id, 'event': 'started'})}\n\n"
            
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: engine.send_message(message, context_params or {})
            )
            
            while ctx.is_active:
                event = ctx.wait_for_event(timeout=0.5)
                if event:
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("event") == "stream_finished":
                        break
                    elif event.get("event") == "error":
                        break
                else:
                    if engine and not engine._is_streaming:
                        break
            
            self._persist_current_session_from_engine(engine)
            final_content = ctx.get_content()
            yield f"data: {json.dumps({'event': 'complete', 'content': final_content, 'stream_id': stream_id})}\n\n"
        
        except Exception as e:
            logger.exception(f"[GatewayLocal] chat_stream 错误: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            if hasattr(ctx, 'context_id') and ctx.context_id:
                self._context_registry.remove_context(ctx.context_id)
            self._active_streams.pop(stream_id, None)
            if ctx:
                ctx.set_active(False)
    
    def _handle_engine_event(self, stream_id: str, event_name: str, *args, **kwargs) -> None:
        ctx = self._active_streams.get(stream_id)
        if not ctx or not ctx.is_active:
            return
        
        event_data = {"stream_id": stream_id, "event": event_name}
        
        if event_name == "content" and args:
            piece = args[0] if args else ""
            ctx.append_content(piece)
            event_data["data"] = {"piece": piece}
        elif event_name == "tool_call_started":
            tool_call_id = args[0] if len(args) > 0 else ""
            tool_name = args[1] if len(args) > 1 else ""
            arguments = args[2] if len(args) > 2 else {}
            event_data["data"] = {"tool_call_id": tool_call_id, "tool_name": tool_name, "arguments": arguments}
        elif event_name == "tool_result":
            tool_call_id = args[0] if len(args) > 0 else ""
            tool_name = args[1] if len(args) > 1 else ""
            result = args[2] if len(args) > 2 else ""
            event_data["data"] = {"tool_call_id": tool_call_id, "tool_name": tool_name, "result": str(result)}
        elif event_name == "stream_finished":
            ctx.buffer["finished"] = True
            final_content = ctx.get_content()
            event_data["data"] = {"content": final_content, "finished": True}
            ctx.set_active(False)
        elif event_name == "error":
            error_msg = args[0] if args else str(kwargs.get("error", "Unknown error"))
            event_data["data"] = {"error": error_msg}
            ctx.set_active(False)
        elif event_name == "permission":
            tool_call_id = args[0] if len(args) > 0 else ""
            if ctx.engine:
                ctx.engine.approve_tool_permission(tool_call_id, True)
            return
        elif event_name == "messages_updated":
            messages = args[0] if args else []
            if ctx.engine:
                session = ctx.engine._session_manager.get_current_session()
                if session:
                    session.set_messages(messages, preserve_compaction=True)
                    logger.debug(f"[GatewayLocal] 消息已更新到 session: {len(messages)} 条")
            return
        
        ctx.push_event(event_data)
    
    def stop_stream(self, stream_id: Optional[str] = None) -> bool:
        target_id = stream_id
        if not target_id:
            target_id = next(
                (sid for sid, ctx in self._active_streams.items() if ctx.is_active),
                None
            )
        if not target_id or target_id not in self._active_streams:
            return False
        
        ctx = self._active_streams[target_id]
        ctx.set_active(False)
        if ctx.engine:
            ctx.engine.stop()
        if ctx.engine:
            self._persist_current_session_from_engine(ctx.engine)
        self._active_streams.pop(target_id, None)
        logger.info(f"[GatewayLocal] 已停止流请求: {target_id}")
        return True
    
    def get_active_streams(self) -> List[str]:
        return [sid for sid, ctx in self._active_streams.items() if ctx.is_active]
