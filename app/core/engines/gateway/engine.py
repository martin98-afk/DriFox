# -*- coding: utf-8 -*-
"""
GatewayEngine — Gateway 专用引擎，与 UI 的 ChatEngine 完全独立

Issue #144: 多用户并发对话支持

职责：
1. 独立管理 Gateway 的 ChatSession（不碰 UI 的 SessionManager.current_index）
2. 处理 Gateway 命令（/help, /new, /model, /agent, /session 等）
3. 处理 AI 对话（每个用户独立 Executor，支持并发）
4. 将 Gateway 会话保存到 SQLite（供 UI 历史列表查看）

设计原则：
- 与 ChatEngine 完全独立：各自有自己的 SessionManager、_is_streaming、_current_worker
- 单例模式：由 Backend 持有，多个 UI 窗口共享同一个 GatewayEngine
- ✅ 每个用户独立 ConversationCore + ConversationExecutor（支持并发）
- 共享 ToolExecutor / AgentManager（无状态组件）
- 空闲 Executor 可被定期清理
"""
import time
from typing import Callable, Dict, List, Optional, Any

from PyQt5.QtCore import QObject, pyqtSignal
from loguru import logger

from app.core.chat_session import ChatSession, SessionManager
from app.core.conversation.core import ConversationCore
from app.core.conversation.config import ConversationConfig, PermissionStrategy
from app.core.conversation.executor import ConversationExecutor
from app.core.conversation.adapters import GatewayConversationAdapter
from app.core.engines.base import BaseEngine
from app.tools import get_builtin_tools_schema
from app.utils.config import Settings


def _noop(*args, **kwargs):
    """空操作回调"""
    pass


DEFAULT_IDLE_TIMEOUT_SECONDS = 300


class GatewayEngine(QObject, BaseEngine):

    worker_started = pyqtSignal()
    worker_finished = pyqtSignal(str)
    worker_error = pyqtSignal(str)

    _global_instance: Optional["GatewayEngine"] = None

    def __init__(
        self,
        get_model_config: Callable[[], Dict[str, Any]],
        tool_executor: Any = None,
        agent_manager: Any = None,
        session_store: Any = None,
        parent: QObject = None,
    ):
        if self._global_instance is not None and self is not self._global_instance:
            raise RuntimeError("GatewayEngine is singleton, use GatewayEngine.get_instance()")

        super().__init__(parent)

        self._get_model_config = get_model_config
        self._tool_executor = tool_executor
        self._agent_manager = agent_manager
        self._session_store = session_store

        # Per-user dictionaries (key: session.session_id)
        self._user_cores: Dict[str, ConversationCore] = {}
        self._user_executors: Dict[str, ConversationExecutor] = {}
        self._user_adapters: Dict[str, GatewayConversationAdapter] = {}
        self._user_pending_queues: Dict[str, List[tuple]] = {}
        self._user_last_active: Dict[str, float] = {}

        # Global session index
        self._all_sessions: Dict[str, ChatSession] = {}
        self._current_agent: Optional[str] = "plan"

        GatewayEngine._global_instance = self

    @classmethod
    def get_instance(
        cls,
        get_model_config: Callable[[], Dict[str, Any]] = None,
        tool_executor: Any = None,
        agent_manager: Any = None,
        session_store: Any = None,
    ) -> "GatewayEngine":
        if cls._global_instance is not None:
            return cls._global_instance
        if get_model_config is None:
            raise ValueError("First call to get_instance() must provide get_model_config")
        instance = cls(get_model_config, tool_executor, agent_manager, session_store)
        cls._global_instance._global_instance = cls._global_instance
        return instance

    def _get_or_create_executor(self, session_id: str) -> ConversationExecutor:
        if session_id not in self._user_executors:
            core = ConversationCore.create(
                get_model_config=self._get_model_config,
                agent_manager=self._agent_manager,
                backend=None,
            )
            self._user_cores[session_id] = core

            config = ConversationConfig(
                permission_strategy=PermissionStrategy.AGENT_CONFIG,
            )
            executor = ConversationExecutor(
                core=core,
                config=config,
                tool_executor=self._tool_executor,
                agent_manager=self._agent_manager,
            )
            self._user_executors[session_id] = executor

            adapter = GatewayConversationAdapter(
                core=core,
                executor=executor,
            )
            self._user_adapters[session_id] = adapter
            self._user_pending_queues[session_id] = []
            self._touch_user(session_id)

            logger.debug(f"[GatewayEngine] Created per-user executor for session={session_id[:12]}")

        return self._user_executors[session_id]

    def _touch_user(self, session_id: str):
        self._user_last_active[session_id] = time.time()

    def get_current_session(self) -> Optional[ChatSession]:
        if not self._all_sessions:
            return None
        active = sorted(
            self._all_sessions.values(),
            key=lambda s: self._user_last_active.get(s.session_id, 0),
            reverse=True,
        )
        return active[0] if active else None

    def add_session(self, session: ChatSession) -> None:
        self._all_sessions[session.session_id] = session
        self._get_or_create_executor(session.session_id)
        self._save_to_store(session)

    def find_session(self, session_id: str) -> Optional[ChatSession]:
        session = self._all_sessions.get(session_id)
        if session:
            self._touch_user(session_id)
        return session

    def switch_to_session(self, session_id: str) -> Optional[ChatSession]:
        session = self._all_sessions.get(session_id)
        if session:
            self._touch_user(session_id)
        return session

    def get_all_sessions(self) -> List[ChatSession]:
        return list(self._all_sessions.values())

    def process(
        self,
        session: ChatSession,
        text: str,
        callbacks: Optional[Dict[str, Callable]] = None,
    ) -> bool:
        callbacks = callbacks or {}

        if session.session_id not in self._all_sessions:
            self.add_session(session)

        self._touch_user(session.session_id)

        if text.startswith("/"):
            response = self._handle_command(session, text)
            if response is not None:
                cb = callbacks.get("stream_finished")
                if cb:
                    cb(response)
                self._save_to_store(session)
                return True

        return self._send_to_ai(session, text, callbacks)

    def _handle_command(self, session: ChatSession, text: str) -> Optional[str]:
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0][1:].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("new", "reset"):
            session.clear()
            return "✅ 会话已重置，开始新的对话！"
        elif cmd == "clear":
            session.clear()
            return "✅ 聊天记录已清除！"
        elif cmd == "help":
            return self._help_text()
        elif cmd == "model":
            return self._cmd_model(args, session)
        elif cmd == "agent":
            return self._cmd_agent(args, session)
        elif cmd == "session":
            return self._cmd_session(args)
        else:
            return None

    def _help_text(self) -> str:
        return "🤖 **DriFox Gateway 命令**\n\n**会话管理**\n- `/new` 或 `/reset` — 重置当前会话\n- `/clear` — 清空聊天记录\n- `/session` — 列出所有历史会话\n- `/session <id>` — 切换到指定会话\n\n**模型 & Agent**\n- `/model` — 查看服务商及当前模型\n- `/agent` — 查看当前 Agent\n- `/help` — 显示此帮助"

    def _cmd_model(self, args: str, session: ChatSession) -> str:
        cfg = Settings.get_instance()
        saved_providers = cfg.llm_saved_providers.value or {}
        if not args:
            if not saved_providers:
                return "📋 暂无配置的服务商"
            lines = ["📋 **可用模型**:\n"]
            for name in sorted(saved_providers.keys()):
                model_name = saved_providers[name].get("模型名称", "")
                lines.append(f"**{name}** — `{model_name}`")
            return "\n".join(lines)
        return f"✅ 已切换到模型配置"

    def _cmd_agent(self, args: str, session: ChatSession) -> str:
        if not self._agent_manager:
            return "❌ Agent 管理器不可用。"
        if not args:
            agents = self._agent_manager.list_primary_agents()
            if not agents:
                return "📋 没有可用的 Agent。"
            current = session.metadata.get("agent") or self._current_agent or "plan"
            lines = [f"📋 **可用 Agent** ({len(agents)} 个):\n"]
            for a in agents:
                marker = " ◀ 当前" if a.name == current else ""
                lines.append(f"- **{a.name}**{marker}: {a.description}")
            return "\n".join(lines)
        agent = self._agent_manager.get_agent(args)
        if agent:
            session.metadata["agent"] = args
            return f"✅ 当前会话已切换到 Agent: `{args}`"
        return f"❌ 未找到 Agent `{args}`"

    def _cmd_session(self, args: str) -> str:
        if not args:
            sessions = self.get_all_sessions()
            if not sessions:
                return "📋 没有历史会话。"
            lines = [f"📋 **Gateway 会话** ({len(sessions)} 个):\n"]
            for s in sessions:
                name = s.name or s.session_id[:12]
                msg_count = s.message_count
                lines.append(f"- `{s.session_id[:12]}...` **{name}** ({msg_count} 条)")
            return "\n".join(lines)
        candidates = [s for s in self.get_all_sessions()
                     if args in s.session_id or args in (s.name or "")]
        if len(candidates) == 1:
            self.switch_to_session(candidates[0].session_id)
            return f"✅ 已切换到会话: **{candidates[0].name}**"
        elif len(candidates) > 1:
            lines = ["找到多个匹配:\n"]
            for s in candidates:
                lines.append(f"- `{s.session_id[:12]}...` **{s.name}**")
            return "\n".join(lines)
        return f"❌ 未找到匹配的会话 `{args}`"

    def _send_to_ai(
        self,
        session: ChatSession,
        text: str,
        callbacks: Dict[str, Callable],
    ) -> bool:
        session_id = session.session_id
        executor = self._get_or_create_executor(session_id)
        adapter = self._user_adapters[session_id]
        self._touch_user(session_id)

        if executor.is_streaming:
            self._user_pending_queues[session_id].append((session, text, callbacks))
            logger.info(f"[GatewayEngine] User={session_id[:12]} message queued "
                       f"({len(self._user_pending_queues[session_id])} pending)")
            return True

        session.add_user_message(content=text)
        llm_config = self._get_model_config()
        if not llm_config:
            logger.error("[GatewayEngine] No model config available")
            cb = callbacks.get("error")
            if cb:
                cb("模型配置无效，请检查设置。")
            return False

        messages = self._build_messages(session, llm_config)
        tools = self._get_tools(session=session)
        wrapped_callbacks = self._make_gateway_callbacks(session, callbacks)

        adapter.set_callbacks(callbacks)
        success = executor.execute(
            messages=messages,
            llm_config=llm_config,
            tools=tools,
            callbacks=wrapped_callbacks,
        )

        if success:
            self.worker_started.emit()
        return success

    def _make_gateway_callbacks(
        self,
        session: ChatSession,
        callbacks: Dict[str, Callable],
    ) -> Dict[str, Callable]:
        session_id = session.session_id
        cb_content = callbacks.get("content_received", _noop)
        cb_finished = callbacks.get("stream_finished", _noop)
        cb_error = callbacks.get("error", _noop)
        chunks = []

        def on_content(chunk: str):
            chunks.append(chunk)
            cb_content(chunk)

        def on_finished(response: str):
            final_response = response or "".join(chunks)
            if final_response.strip():
                session.add_assistant_message(content=final_response)
            self._save_to_store(session)
            cb_finished(final_response)
            self.worker_finished.emit(final_response)
            self._process_next(session_id)

        def on_error(error: str):
            cb_error(error)
            self.worker_error.emit(error)
            self._process_next(session_id)

        result = {
            "content_received": on_content,
            "finished": on_finished,
            "error": on_error,
        }

        cb_tool_call = callbacks.get("tool_call_started")
        cb_tool_result = callbacks.get("tool_result_received")
        if cb_tool_call:
            result["tool_call_started"] = lambda i, n, a, r: cb_tool_call({"tool_name": n, "arguments": a})
        if cb_tool_result:
            result["tool_result_received"] = lambda i, n, a, r: cb_tool_result({"tool_name": n, "result": r})

        def on_messages_updated(messages):
            if messages:
                session.set_messages(messages, preserve_compaction=True)
                self._save_to_store(session)
        result["messages_updated"] = on_messages_updated

        return result

    def _process_next(self, session_id: str) -> None:
        queue = self._user_pending_queues.get(session_id, [])
        if not queue:
            return
        session, text, callbacks = queue.pop(0)
        logger.info(f"[GatewayEngine] Processing queued message for user={session_id[:12]} "
                   f"({len(queue)} remaining)")
        self._send_to_ai(session, text, callbacks)

    def cleanup_idle_executors(self, idle_timeout: int = DEFAULT_IDLE_TIMEOUT_SECONDS):
        now = time.time()
        idle_sessions = []

        for session_id, last_active in self._user_last_active.items():
            if now - last_active > idle_timeout:
                executor = self._user_executors.get(session_id)
                if executor and not executor.is_streaming:
                    idle_sessions.append(session_id)

        for session_id in idle_sessions:
            self._cleanup_user(session_id)

        if idle_sessions:
            logger.info(f"[GatewayEngine] Cleaned up {len(idle_sessions)} idle executors")

    def _cleanup_user(self, session_id: str):
        executor = self._user_executors.pop(session_id, None)
        if executor:
            try:
                executor.cleanup()
            except Exception as e:
                logger.warning(f"[GatewayEngine] Executor cleanup error for {session_id[:12]}: {e}")
        self._user_adapters.pop(session_id, None)
        self._user_cores.pop(session_id, None)
        self._user_pending_queues.pop(session_id, None)
        self._user_last_active.pop(session_id, None)

    def get_active_user_count(self) -> int:
        return len(self._user_executors)

    def _build_messages(self, session: ChatSession, llm_config: Dict) -> List[Dict]:
        agent_name = session.metadata.get("agent") or self._current_agent or "plan"
        messages = []
        if self._agent_manager:
            system_prompt = self._agent_manager.get_agent_system_prompt(agent_name)
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
        messages.extend(session.get_context_messages())
        return messages

    def _get_tools(self, session: Optional[ChatSession] = None) -> List[Dict]:
        agent_name = None
        if self._agent_manager:
            s = session or self.get_current_session()
            if s:
                agent_name = s.metadata.get("agent") or self._current_agent
        if agent_name and self._agent_manager:
            tools = self._agent_manager.get_agent_tools_schema(agent_name)
        else:
            tools = get_builtin_tools_schema(
                agent_manager=self._agent_manager,
                builtin_tools=self._tool_executor._builtin_tools if self._tool_executor else None,
            )
        from app.core.conversation.config import filter_interactive_tools, PermissionStrategy
        return filter_interactive_tools(tools, PermissionStrategy.AGENT_CONFIG)

    @staticmethod
    def _is_recent_message(msg: Dict, threshold_seconds: int = 3) -> bool:
        from datetime import datetime
        ts = msg.get("timestamp", "")
        if not ts:
            return False
        try:
            msg_time = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            return (datetime.now() - msg_time).total_seconds() < threshold_seconds
        except (ValueError, TypeError):
            return False

    def _save_to_store(self, session: ChatSession) -> None:
        if not self._session_store:
            return
        try:
            self._session_store.save_session(session.to_dict())
        except Exception as e:
            logger.warning(f"[GatewayEngine] Failed to save session: {e}")