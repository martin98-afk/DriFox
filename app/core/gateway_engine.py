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
from app.core.conversation.core import ConversationCore
from app.core.conversation.config import ConversationConfig, PermissionStrategy
from app.core.conversation.adapters import GatewayConversationAdapter
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

        # ===== ConversationCore（聚合 SessionManager + Compactor + PermissionCache）=====
        self._conversation_core = ConversationCore.create(
            get_model_config=get_model_config,
            agent_manager=agent_manager,
            backend=None,  # Gateway 不需要记忆上下文
        )

        # ===== ConversationExecutor（统一 Worker 执行）=====
        from app.core.conversation.executor import ConversationExecutor
        config = ConversationConfig(
            permission_strategy=PermissionStrategy.AGENT_CONFIG,
        )
        self._conversation_executor = ConversationExecutor(
            core=self._conversation_core,
            config=config,
            tool_executor=tool_executor,
            agent_manager=agent_manager,
        )

        # ===== GatewayConversationAdapter（直接回调适配器）=====
        self._adapter = GatewayConversationAdapter(
            core=self._conversation_core,
            executor=self._conversation_executor,
        )

        # ===== 共享的无状态组件 =====
        self._get_model_config = get_model_config
        self._tool_executor = tool_executor
        self._agent_manager = agent_manager
        self._session_store = session_store

        # ===== 消息队列（流式中到达的新消息排队依次处理）=====
        self._pending_queue: List[tuple] = []  # [(session, text, callbacks), ...]

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
        sm = self._conversation_core.session_manager
        sm.sessions.append(session)
        sm._touch_session(session.session_id)
        self._save_to_store(session)

    def find_session(self, session_id: str) -> Optional[ChatSession]:
        """查找 Gateway 会话"""
        sm = self._conversation_core.session_manager
        for s in sm.sessions:
            if s.session_id == session_id:
                sm._touch_session(s.session_id)
                return s
        return None

    def switch_to_session(self, session_id: str) -> Optional[ChatSession]:
        """切换当前 Gateway 会话（不影响 UI）"""
        sm = self._conversation_core.session_manager
        for idx, s in enumerate(sm.sessions):
            if s.session_id == session_id:
                sm.switch_to_session(idx)
                return s
        return None

    def get_current_session(self) -> Optional[ChatSession]:
        """获取当前 Gateway 会话"""
        return self._conversation_core.session_manager.get_current_session()

    def get_all_sessions(self) -> List[ChatSession]:
        """获取所有 Gateway 会话"""
        return self._conversation_core.session_manager.get_all_sessions()

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
- `/model` — 查看所有服务商及当前模型
- `/model 服务商名` — 查看该服务商的可用模型
- `/model 服务商名 模型名` — 切换服务商和模型
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

        # 当前选中的服务商和模型
        current_provider = cfg.llm_selected_model.value or cfg.llm_model.value or ""
        saved_providers = cfg.llm_saved_providers.value or {}

        # 会话级覆盖
        session_meta = session.metadata.get("model") or ""
        # session_meta 格式：{"provider": "...", "model": "..."} 或旧格式字符串
        session_provider = session_meta.get("provider", "") if isinstance(session_meta, dict) else ""
        session_model = session_meta.get("model", "") if isinstance(session_meta, dict) else session_meta

        if not args:
            # 列出所有服务商及其模型
            if not saved_providers:
                return "📋 **当前模型**: 暂无配置的服务商\n请在桌面端设置中添加服务商。"

            lines = ["📋 **可用模型**:\n"]
            for name in sorted(saved_providers.keys()):
                models = saved_providers[name].get("模型列表", [])
                model_name = saved_providers[name].get("模型名称", "")
                marker = " ◀ 当前服务商" if name == current_provider else ""
                session_marker = " ⚡ 会话覆盖" if name == session_provider else ""

                if models:
                    lines.append(f"**{name}**{marker}{session_marker}")
                    lines.append(f"  模型: `{model_name}`")
                    if len(models) > 1:
                        lines.append(f"  可选: {', '.join(f'`{m}`' for m in models[:8])}")
                else:
                    lines.append(f"**{name}**{marker}{session_marker} — `{model_name}`")

                if session_provider == name:
                    lines.append(f"  ⚡ 会话级模型: `{session_model}`")

                lines.append("")

            return "\n".join(lines).strip()

        # 解析参数：/model 服务商名 [模型名]
        parts = args.strip().split(maxsplit=1)
        provider_name = parts[0]
        model_name = parts[1] if len(parts) > 1 else ""

        # 模糊匹配服务商
        matches = [n for n in saved_providers if provider_name.lower() in n.lower()]
        if not matches:
            return f"❌ 未找到服务商 `{provider_name}`\n发送 `/model` 查看服务商列表。"

        provider = matches[0]
        config = saved_providers[provider]

        if not model_name:
            # 只指定了服务商，显示该服务商的模型
            model_name = config.get("模型名称", "")
            available = config.get("模型列表", [])
            if available:
                opt_list = ", ".join(f"`{m}`" for m in available[:10])
                return (f"📋 **{provider}** 当前模型: `{model_name}`\n"
                        f"可选: {opt_list}")
            return f"📋 **{provider}** 当前模型: `{model_name}`"

        # 指定了服务商 + 模型，切换
        if provider not in saved_providers:
            return f"❌ 未找到服务商 `{provider}`"

        # 设置会话级覆盖
        session.metadata["model"] = {"provider": provider, "model": model_name}
        return (f"✅ 已切换到 **{provider}** 的模型: `{model_name}`\n"
                f"（仅当前 Gateway 会话生效）")

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
        sm = self._conversation_core.session_manager
        if not args:
            # 列出所有 Gateway 会话
            sessions = sm.get_all_sessions()
            if not sessions:
                return "📋 没有历史会话。"

            current = sm.get_current_session()
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
        candidates = [s for s in sm.get_all_sessions()
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
        """发送消息到 AI（使用 ConversationExecutor）"""
        if self._conversation_executor.is_streaming:
            # 排队，不丢弃消息
            self._pending_queue.append((session, text, callbacks))
            logger.info(f"[GatewayEngine] Message queued ({len(self._pending_queue)} pending)")
            return True

        # 添加用户消息
        session.add_user_message(content=text)

        # 获取模型配置
        llm_config = self._get_model_config()
        if not llm_config:
            logger.error("[GatewayEngine] No model config available")
            cb = callbacks.get("error")
            if cb:
                cb("模型配置无效，请检查设置。")
            return False

        # 会话级模型覆盖
        session_model_meta = session.metadata.get("model")
        if session_model_meta:
            # 新格式：{"provider": "...", "model": "..."}
            if isinstance(session_model_meta, dict):
                override_provider = session_model_meta.get("provider", "")
                override_model = session_model_meta.get("model", "")
                if override_provider and override_model:
                    llm_config = {
                        **llm_config,
                        "model": override_model,
                    }
                    # provider 覆盖需要替换 api_base 等
                    saved_providers = Settings.get_instance().llm_saved_providers.value or {}
                    if override_provider in saved_providers:
                        provider_cfg = saved_providers[override_provider].copy()
                        provider_cfg.pop("备注", None)
                        provider_cfg.pop("获取地址", None)
                        provider_cfg.pop("模型列表", None)
                        provider_cfg["模型名称"] = override_model
                        llm_config = provider_cfg
            # 旧格式：直接是模型名
            elif isinstance(session_model_meta, str) and session_model_meta:
                llm_config = {**llm_config, "model": session_model_meta}

        # 构建消息
        messages = self._build_messages(session, llm_config)

        # 获取工具
        tools = self._get_tools(session=session)

        # 将 Gateway 特有逻辑（session保存、队列处理）包装在回调中
        wrapped_callbacks = self._make_gateway_callbacks(session, callbacks)

        # 使用统一 Executor 执行
        self._adapter.set_callbacks(callbacks)
        success = self._conversation_executor.execute(
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
        """包装 Gateway 特有逻辑的回调"""
        from app.core.message_content import consolidate_messages

        cb_content = callbacks.get("content_received", _noop)
        cb_finished = callbacks.get("stream_finished", _noop)
        cb_error = callbacks.get("error", _noop)

        chunks = []

        def on_content(chunk: str):
            chunks.append(chunk)
            cb_content(chunk)

        def on_finished(response: str):
            """AI 完成 — 保存会话并处理队列"""
            final_response = response or "".join(chunks)
            if final_response.strip():
                session.add_assistant_message(content=final_response)
            self._save_to_store(session)
            cb_finished(final_response)
            self.worker_finished.emit(final_response)
            self._process_next()

        def on_error(error: str):
            """错误 — 处理队列"""
            cb_error(error)
            self.worker_error.emit(error)
            self._process_next()

        result = {
            "content_received": on_content,
            "finished": on_finished,
            "error": on_error,
        }

        # 工具调用跟踪回调
        cb_tool_call = callbacks.get("tool_call_started")
        cb_tool_result = callbacks.get("tool_result_received")
        if cb_tool_call:
            result["tool_call_started"] = lambda i, n, a, r: cb_tool_call({"tool_name": n, "arguments": a})
        if cb_tool_result:
            result["tool_result_received"] = lambda i, n, a, r: cb_tool_result({"tool_name": n, "result": r})

        # 消息更新 → 保存到会话
        def on_messages_updated(messages):
            if messages:
                session.set_messages(messages, preserve_compaction=True)
                self._save_to_store(session)
        result["messages_updated"] = on_messages_updated

        return result

    def _process_next(self) -> None:
        """从队列中取出下一条消息处理"""
        if not self._pending_queue:
            return
        session, text, callbacks = self._pending_queue.pop(0)
        logger.info(f"[GatewayEngine] Processing queued message ({len(self._pending_queue)} remaining)")
        self._send_to_ai(session, text, callbacks)

    def _build_messages(self, session: ChatSession, llm_config: Dict) -> List[Dict]:
        """构建消息列表"""
        agent_name = session.metadata.get("agent") or self._current_agent or "plan"
        messages = []

        # Agent 系统提示词（使用 get_agent_system_prompt 获取完整格式化提示）
        if self._agent_manager:
            system_prompt = self._agent_manager.get_agent_system_prompt(agent_name)
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})

        # Gateway 专用约束（追加在 agent 提示词后面，覆盖主智能体中对 question 的推荐）
        gateway_constraints = """
## Gateway 模式约束
你正在通过 Gateway 对外提供 AI 服务（当前平台可能为钉钉/企业微信等）。
以下是必须遵守的规则：

### 【禁止使用的工具】
- ❌ **禁止** 使用 `question` 工具 —— 这是交互式提问工具，Gateway 模式下无法与用户交互
- ❌ **禁止** 使用 `task_batch` 和 `task_status` 工具 —— 不支持发布子智能体任务
- ❌ **禁止** 使用 `todowrite` 工具 —— 不支持待办事项功能

### 【必须遵守】
- 所有任务必须**一次性完成**，不支持中途暂停、等待确认或来回交互
- 如果信息不足，使用 `websearch` 或 `webfetch` 自行搜索，不要问用户
- 直接输出最终结果，不要问"是否需要"、"是否继续"之类的问题
- 回答要简洁、完整、可直接使用
"""
        if system_prompt:
            # 追加到现有 system prompt 后面
            messages[0]["content"] = messages[0]["content"] + "\n\n" + gateway_constraints.strip()
        else:
            messages.append({"role": "system", "content": gateway_constraints.strip()})

        # 会话自身消息
        messages.extend(session.get_context_messages())

        return messages

    def _get_tools(self, session: Optional[ChatSession] = None) -> List[Dict]:
        """获取工具 schema（过滤掉 Gateway 不适用的交互式工具）

        Args:
            session: 当前处理的会话（用于获取 agent 名称）
        """
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

        # 过滤掉 Gateway 不适用的交互式工具
        before = len(tools)
        filtered = [t for t in tools if t.get("function", {}).get("name") not in GATEWAY_DISABLED_TOOLS
                    and t.get("name") not in GATEWAY_DISABLED_TOOLS]
        removed = before - len(filtered)
        if removed > 0:
            removed_names = [t.get("function", {}).get("name") or t.get("name", "?") for t in tools
                             if t.get("function", {}).get("name") in GATEWAY_DISABLED_TOOLS
                             or t.get("name") in GATEWAY_DISABLED_TOOLS]
            logger.info(f"[GatewayEngine] Filtered {removed} tool(s): {removed_names}")
        return filtered

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
        """清理当前 worker（委托 ConversationExecutor）"""
        self._conversation_executor.cleanup()
