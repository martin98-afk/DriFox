# -*- coding: utf-8 -*-
"""
GatewayEngine - Gateway 专用引擎，与 UI 的 ChatEngine 完全独立

职责：
1. 独立管理 Gateway 的 ChatSession（不碰 UI 的 SessionManager.current_index）
2. 处理 Gateway 命令（/help, /new, /model, /agent, /session 等）
3. 处理 AI 对话（独立 worker，不与 UI 争抢 _is_streaming）
4. 将 Gateway 会话保存到 SQLite（供 UI 历史列表查看）

设计原则：
- 与 ChatEngine 完全独立：各自有自己的 SessionManager、_is_streaming、_current_worker
- 单例模式：由 Backend 持有，多个 UI 窗口共享同一个 GatewayEngine
- 一次只处理一个 Gateway 消息（单 worker 串行）
- 共享 ToolExecutor / AgentManager（无状态组件）
"""

from typing import Callable, Dict, List, Optional, Any

from PyQt5.QtCore import QObject, pyqtSignal
from loguru import logger

from app.core.chat_session import ChatSession, SessionManager
from app.core.context_builder import ContextBudgetAllocator
from app.core.history_compactor import HistoryCompactor
from app.core.permission_cache import PermissionCache
from app.core.workers import OpenAIChatWorker
from app.tools import get_builtin_tools_schema
from app.utils.config import Settings


# Gateway 模式下禁用的工具（交互式/不适用于网关场景）
GATEWAY_DISABLED_TOOLS = {"question", "task_batch", "task_status"}


def _noop(*args, **kwargs):
    """空操作回调"""
    pass


class GatewayEngine(QObject):
    """Gateway 专用引擎，与 UI 的 ChatEngine 完全独立"""

    # 状态信号
    worker_started = pyqtSignal()
    worker_finished = pyqtSignal(str)  # response text
    worker_error = pyqtSignal(str)     # error message

    # 全局单例
    _global_instance: Optional["GatewayEngine"] = None

    def __init__(
        self,
        get_model_config: Callable[[], Dict[str, Any]],
        tool_executor: Any = None,
        agent_manager: Any = None,
        session_store: Any = None,
        parent: QObject = None,
    ):
        super().__init__(parent)

        # 防止重复构造单例
        if self._global_instance is not None and self is not self._global_instance:
            raise RuntimeError("GatewayEngine is singleton, use GatewayEngine.get_instance()")

        # ===== 独立的组件 =====
        self._session_manager = SessionManager()  # 不碰 UI 的 SessionManager
        self._compactor = HistoryCompactor(
            get_model_config=get_model_config,
            agent_manager=agent_manager,
        )
        self._permission_cache = PermissionCache()

        # ===== 共享的无状态组件 =====
        self._get_model_config = get_model_config
        self._tool_executor = tool_executor
        self._agent_manager = agent_manager
        self._session_store = session_store

        # ===== 自己的流式状态 =====
        self._current_worker: Optional[OpenAIChatWorker] = None
        self._is_streaming = False

        # ===== Gateway 会话级配置 =====
        self._current_agent: Optional[str] = "plan"

        # 注册为全局单例
        GatewayEngine._global_instance = self

    @classmethod
    def get_instance(
        cls,
        get_model_config: Callable[[], Dict[str, Any]] = None,
        tool_executor: Any = None,
        agent_manager: Any = None,
        session_store: Any = None,
    ) -> "GatewayEngine":
        """获取全局单例"""
        if cls._global_instance is not None:
            return cls._global_instance
        if get_model_config is None:
            raise ValueError("First call to get_instance() must provide get_model_config")
        instance = cls(get_model_config, tool_executor, agent_manager, session_store)
        cls._global_instance._global_instance = cls._global_instance
        return instance

    # ==================== 会话管理 ====================

    def add_session(self, session: ChatSession) -> None:
        """添加 Gateway 会话，不改变 current_index"""
        self._session_manager.sessions.append(session)
        self._session_manager._touch_session(session.session_id)
        self._save_to_store(session)

    def find_session(self, session_id: str) -> Optional[ChatSession]:
        """查找 Gateway 会话"""
        for s in self._session_manager.sessions:
            if s.session_id == session_id:
                self._session_manager._touch_session(s.session_id)
                return s
        return None

    def switch_to_session(self, session_id: str) -> Optional[ChatSession]:
        """切换当前 Gateway 会话（不影响 UI）"""
        for idx, s in enumerate(self._session_manager.sessions):
            if s.session_id == session_id:
                self._session_manager.switch_to_session(idx)
                return s
        return None

    def get_current_session(self) -> Optional[ChatSession]:
        """获取当前 Gateway 会话"""
        return self._session_manager.get_current_session()

    def get_all_sessions(self) -> List[ChatSession]:
        """获取所有 Gateway 会话"""
        return self._session_manager.get_all_sessions()

    # ==================== 入口 ====================

    def process(
        self,
        session: ChatSession,
        text: str,
        callbacks: Optional[Dict[str, Callable]] = None,
    ) -> bool:
        """
        处理 Gateway 消息

        Args:
            session: ChatSession（Gateway 自己的）
            text: 消息文本
            callbacks: 回调字典 {
                "content_received": fn(chunk),
                "stream_finished": fn(response),
                "error": fn(error),
            }

        Returns:
            True 如果消息已被处理
        """
        callbacks = callbacks or {}

        if text.startswith("/"):
            response = self._handle_command(session, text)
            if response is not None:
                # 命令已处理
                cb = callbacks.get("stream_finished")
                if cb:
                    cb(response)
                self._save_to_store(session)
                return True
            # 命令返回 None = 当作 AI 消息处理（未知命令）
            # fall through to AI

        return self._send_to_ai(session, text, callbacks)

    # ==================== 命令处理 ====================

    def _handle_command(self, session: ChatSession, text: str) -> Optional[str]:
        """
        处理命令，返回响应文本或 None（未知命令）

        支持的命令：
        /new, /reset     — 重置当前会话
        /clear           — 清空当前会话
        /help            — 显示帮助
        /model           — 查看当前模型
        /model xxx       — 切换模型
        /agent           — 查看当前 agent
        /agent xxx       — 切换 agent
        /session         — 列出所有 Gateway 会话
        /session xxx     — 切换到指定会话
        """
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
            return None  # 未知命令，交给 AI

    def _help_text(self) -> str:
        return """🤖 **DriFox Gateway 命令**

**会话管理**
- `/new` 或 `/reset` — 重置当前会话，开始新的对话
- `/clear` — 清空当前会话的聊天记录
- `/session` — 列出所有历史会话
- `/session <id>` — 切换到指定会话

**模型 & Agent**
- `/model` — 查看当前使用的模型
- `/model <名称>` — 切换到指定模型
- `/agent` — 查看当前使用的 Agent
- `/agent <名称>` — 切换到指定 Agent

**通用**
- `/help` — 显示此帮助

--------
💡 Gateway 会话与桌面端完全隔离，互不影响。
   历史会话可在桌面端 UI 列表中查看。"""

    def _cmd_model(self, args: str, session: ChatSession) -> str:
        """处理 /model 命令"""
        cfg = Settings.get_instance()
        providers = cfg.llm_saved_providers.value or {}

        if not args:
            # 列出所有模型
            if not providers:
                current = cfg.llm_selected_model.value or cfg.llm_model.value
                return f"📋 **当前模型**: `{current}`\n\n（无可用模型列表）"

            lines = [f"📋 **可用模型** ({len(providers)} 个):\n"]
            current = cfg.llm_selected_model.value or ""
            session_model = session.metadata.get("model", "")

            for name in sorted(providers.keys()):
                marker = " ◀ 当前" if name == current else ""
                session_marker = " ⚡ 会话级" if name == session_model else ""
                lines.append(f"- `{name}`{marker}{session_marker}")

            if session_model and session_model != current:
                lines.append(f"\n⚡ 会话级覆盖模型: `{session_model}`")
                lines.append("（发送 `/model` 取消覆盖，使用全局模型）")

            return "\n".join(lines)

        # /model <name> — 切换到指定模型
        if args in providers:
            # 设置为会话级覆盖
            session.metadata["model"] = args
            return f"✅ 当前会话已切换到模型: `{args}`\n（仅当前 Gateway 会话生效）"
        else:
            # 尝试模糊匹配
            matches = [n for n in providers if args.lower() in n.lower()]
            if matches:
                lines = [f"找到 {len(matches)} 个匹配:\n"]
                for m in matches[:10]:
                    lines.append(f"- `{m}`")
                return "\n".join(lines)
            return f"❌ 未找到模型 `{args}`\n发送 `/model` 查看可用模型列表。"

    def _cmd_agent(self, args: str, session: ChatSession) -> str:
        """处理 /agent 命令"""
        if not self._agent_manager:
            return "❌ Agent 管理器不可用。"

        if not args:
            # 列出所有 primary agent
            agents = self._agent_manager.list_primary_agents()
            if not agents:
                return "📋 没有可用的 Agent。"

            current = session.metadata.get("agent") or self._current_agent or "plan"
            lines = [f"📋 **可用 Agent** ({len(agents)} 个):\n"]
            for a in agents:
                marker = " ◀ 当前" if a.name == current else ""
                lines.append(f"- **{a.name}**{marker}: {a.description}")
            return "\n".join(lines)

        # /agent <name> — 切换到指定 agent
        agent = self._agent_manager.get_agent(args)
        if agent:
            session.metadata["agent"] = args
            return f"✅ 当前会话已切换到 Agent: `{args}`\n（仅当前 Gateway 会话生效）"
        else:
            # 模糊匹配
            agents = self._agent_manager.list_primary_agents()
            matches = [a for a in agents if args.lower() in a.name.lower()]
            if matches:
                lines = [f"找到 {len(matches)} 个匹配:\n"]
                for m in matches[:10]:
                    lines.append(f"- **{m.name}**: {m.description}")
                return "\n".join(lines)
            return f"❌ 未找到 Agent `{args}`\n发送 `/agent` 查看可用列表。"

    def _cmd_session(self, args: str) -> str:
        """处理 /session 命令"""
        if not args:
            # 列出所有 Gateway 会话
            sessions = self._session_manager.get_all_sessions()
            if not sessions:
                return "📋 没有历史会话。"

            current = self._session_manager.get_current_session()
            current_id = current.session_id if current else None

            lines = [f"📋 **Gateway 会话** ({len(sessions)} 个):\n"]
            for s in sessions:
                marker = " ◀ 当前" if s.session_id == current_id else ""
                name = s.name or s.session_id[:12]
                msg_count = s.message_count
                lines.append(f"- `{s.session_id[:12]}...` **{name}** ({msg_count} 条){marker}")
            return "\n".join(lines)

        # /session <id> — 切换到指定会话
        # 支持部分匹配
        candidates = [s for s in self._session_manager.get_all_sessions()
                     if args in s.session_id or args in s.name]
        if len(candidates) == 1:
            self.switch_to_session(candidates[0].session_id)
            return f"✅ 已切换到会话: **{candidates[0].name}**"
        elif len(candidates) > 1:
            lines = [f"找到多个匹配:\n"]
            for s in candidates:
                lines.append(f"- `{s.session_id[:12]}...` **{s.name}**")
            return "\n".join(lines)
        else:
            return f"❌ 未找到匹配的会话 `{args}`"

    # ==================== AI 对话 ====================

    def _send_to_ai(
        self,
        session: ChatSession,
        text: str,
        callbacks: Dict[str, Callable],
    ) -> bool:
        """发送消息到 AI（使用独立 worker）"""
        if self._is_streaming:
            logger.warning("[GatewayEngine] Already streaming, ignoring")
            cb = callbacks.get("error")
            if cb:
                cb("上一个请求正在处理中，请稍候。")
            return False

        # 添加用户消息
        session.add_user_message(content=text)

        # 获取模型配置
        llm_config = self._get_model_config()
        if not llm_config:
            logger.error("[GatewayEngine] No model config available")
            cb = callbacks.get("error")
            if cb:
                cb("模型配置无效，请检查设置。")
            self._is_streaming = False
            return False

        # 会话级模型覆盖
        session_model = session.metadata.get("model")
        if session_model:
            llm_config = {**llm_config, "model": session_model}

        # 构建消息
        messages = self._build_messages(session, llm_config)

        # 获取工具
        tools = self._get_tools()

        # 创建 worker
        try:
            worker = OpenAIChatWorker(
                messages=messages,
                session_messages=session.get_context_messages(),
                llm_config=llm_config,
                tools=tools,
                tool_executor=self._tool_executor,
                tool_start_callback=None,           # Gateway 不需要交互式权限
                permission_check_callback=None,     # Gateway 自动放行
                compaction_prompt="",
                compaction_config={},
                permission_cache=self._permission_cache,
                compactor=self._compactor,          # Gateway 使用自己的 compactor
                initial_compaction_cache=getattr(session, "compaction_cache", None),
            )
        except Exception as e:
            logger.error(f"[GatewayEngine] Failed to create worker: {e}")
            cb = callbacks.get("error")
            if cb:
                cb(f"创建 AI 工作线程失败: {e}")
            return False

        # 连接信号 → 回调
        self._connect_worker_signals(worker, session, callbacks)

        # 启动
        self._current_worker = worker
        self._is_streaming = True
        worker.start()
        self.worker_started.emit()
        return True

    def _build_messages(self, session: ChatSession, llm_config: Dict) -> List[Dict]:
        """构建消息列表"""
        agent_name = session.metadata.get("agent") or self._current_agent or "plan"
        messages = []

        # Agent 系统提示词（使用 get_agent_system_prompt 获取完整格式化提示）
        if self._agent_manager:
            system_prompt = self._agent_manager.get_agent_system_prompt(agent_name)
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})

        # 会话自身消息
        messages.extend(session.get_context_messages())

        return messages

    def _get_tools(self) -> List[Dict]:
        """获取工具 schema（过滤掉 Gateway 不适用的交互式工具）"""
        agent_name = None
        if self._agent_manager:
            session = self.get_current_session()
            if session:
                agent_name = session.metadata.get("agent") or self._current_agent

        if agent_name and self._agent_manager:
            tools = self._agent_manager.get_agent_tools_schema(agent_name)
        else:
            tools = get_builtin_tools_schema(
                agent_manager=self._agent_manager,
                builtin_tools=self._tool_executor._builtin_tools if self._tool_executor else None,
            )

        # 过滤掉 Gateway 不适用的交互式工具
        filtered = [t for t in tools if t.get("function", {}).get("name") not in GATEWAY_DISABLED_TOOLS
                    and t.get("name") not in GATEWAY_DISABLED_TOOLS]
        return filtered

    def _connect_worker_signals(
        self,
        worker: OpenAIChatWorker,
        session: ChatSession,
        callbacks: Dict[str, Callable],
    ) -> None:
        """连接 worker 信号到回调"""
        cb_content = callbacks.get("content_received", _noop)
        cb_finished = callbacks.get("stream_finished", _noop)
        cb_error = callbacks.get("error", _noop)

        # 收集流式内容
        chunks = []

        def on_content(chunk: str):
            chunks.append(chunk)
            cb_content(chunk)

        def on_finished(response: str):
            """AI 完成"""
            self._is_streaming = False
            self._current_worker = None

            # 尝试获取完整的响应
            final_response = response or "".join(chunks)

            # 添加到会话
            if final_response.strip():
                session.add_assistant_message(content=final_response)

            # 保存到 SQLite（供 UI 历史查看）
            self._save_to_store(session)

            cb_finished(final_response)
            self.worker_finished.emit(final_response)

        def on_error(error: str):
            self._is_streaming = False
            self._current_worker = None
            cb_error(error)
            self.worker_error.emit(error)

        worker.content_received.connect(on_content)
        worker.finished_with_content.connect(on_finished)
        worker.error_occurred.connect(on_error)

        # Gateway 不需要 tool_call_started / tool_result_received 等 UI 信号
        # 但需要处理工具调用的结果（自动完成）和 messages_updated
        def on_messages_updated(messages: List[Dict]):
            """工具调用后消息更新"""
            if messages:
                session.set_messages(messages, preserve_compaction=True)
                self._save_to_store(session)

        worker.finished_with_messages.connect(on_messages_updated)

    # ==================== 持久化 ====================

    def _save_to_store(self, session: ChatSession) -> None:
        """保存 Gateway 会话到 SQLite（供 UI 历史查看）"""
        if not self._session_store:
            return
        try:
            self._session_store.save_session(session.to_dict())
        except Exception as e:
            logger.warning(f"[GatewayEngine] Failed to save session: {e}")

    # ==================== 清理 ====================

    def cleanup(self):
        """清理当前 worker"""
        worker = self._current_worker
        self._current_worker = None
        self._is_streaming = False

        if worker:
            try:
                worker.cancel()
                if worker.isRunning():
                    worker.quit()
                worker.cleanup()
            except Exception as e:
                logger.warning(f"[GatewayEngine] Worker cleanup error: {e}")
