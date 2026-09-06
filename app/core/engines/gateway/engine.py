# -*- coding: utf-8 -*-
"""
GatewayEngine — Gateway 专用引擎，与 UI 的 ChatEngine 完全独立

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

from typing import Any, Callable, Dict, List, Optional

from loguru import logger
from PyQt5.QtCore import QObject, pyqtSignal

from app.core.chat_session import ChatSession
from app.core.conversation.adapters import GatewayConversationAdapter
from app.core.conversation.config import ConversationConfig, PermissionStrategy
from app.core.conversation.core import ConversationCore
from app.core.engines.base import BaseEngine
from app.tools import get_builtin_tools_schema
from app.utils.config import Settings


def _noop(*args, **kwargs):
    """空操作回调"""
    pass


class GatewayEngine(QObject, BaseEngine):
    """Gateway 专用引擎，与 UI 的 ChatEngine 完全独立"""

    # 状态信号
    worker_started = pyqtSignal()
    worker_finished = pyqtSignal(str)  # response text
    worker_error = pyqtSignal(str)  # error message

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
        # 防止重复构造单例
        if self._global_instance is not None and self is not self._global_instance:
            raise RuntimeError("GatewayEngine is singleton, use GatewayEngine.get_instance()")

        # 先调用 QObject.__init__（会通过 MRO 触发 BaseEngine.__init__()，使用默认值）
        super().__init__(parent)

        # ===== ConversationCore =====
        self._conversation_core = ConversationCore.create(
            get_model_config=get_model_config,
            agent_manager=agent_manager,
            backend=None,
        )

        # ===== ConversationExecutor =====
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

        # ===== GatewayConversationAdapter =====
        self._adapter = GatewayConversationAdapter(
            core=self._conversation_core,
            executor=self._conversation_executor,
        )

        # ===== 共享的无状态组件 =====
        self._get_model_config = get_model_config
        self._tool_executor = tool_executor
        self._agent_manager = agent_manager
        self._session_store = session_store

        # ===== 消息队列 =====
        self._pending_queue: List[tuple] = []

        # ===== Gateway 会话级配置 =====
        self._current_agent: Optional[str] = "plan"

        # ===== 生命周期状态 =====
        # cleanup() 后置 False；上层（ChatBackend._on_gateway_input）据此跳过该实例，
        # 避免给平台回复"引擎已停用"并让 future 永远挂起。
        self._is_active: bool = True

        # BaseEngine 属性已在上面赋值（_conversation_core, _conversation_executor）
        # 无需再调用 BaseEngine.__init__，因为 super() 链已触发过（使用默认值）

        # 注册为全局单例
        GatewayEngine._global_instance = self

    @property
    def is_active(self) -> bool:
        """引擎是否仍可用（cleanup 之后变 False，外部可据此跳过该实例）。

        多窗口场景下：第一个窗口 cleanup 会把全局单例 _global_instance 置 None，
        第二个窗口启动时重新创建新实例，本属性在新实例上始终为 True。
        单窗口 cleanup 后没新窗口时，旧实例 is_active=False，
        平台残留消息到达 backend 由其入口守卫拦截，不再走到本引擎。
        """
        return self._is_active

    @classmethod
    def get_instance(
        cls,
        get_model_config: Callable[[], Dict[str, Any]] = None,
        tool_executor: Any = None,
        agent_manager: Any = None,
        session_store: Any = None,
    ) -> "GatewayEngine":
        """获取全局单例（首次创建经 EngineRegistry 工厂 — 插件可替换 gateway 引擎）

        插件注册 ENGINE_SLOT_GATEWAY 工厂 → 返回插件类实例（须继承 GatewayEngine，
        isinstance 安全网同 ui 槽位）；未注册 → 内置实现，行为与之前完全一致。
        单例语义保留：插件实例同样写回 _global_instance。
        """
        if cls._global_instance is not None:
            return cls._global_instance
        if get_model_config is None:
            raise ValueError("First call to get_instance() must provide get_model_config")

        from app.plugins.registries.engine_registry import create_engine_for_slot

        instance = create_engine_for_slot(
            "gateway",
            cls,
            get_model_config=get_model_config,
            tool_executor=tool_executor,
            agent_manager=agent_manager,
            session_store=session_store,
        )
        GatewayEngine._global_instance = instance
        return instance

    def cleanup(self):
        """窗口关闭时解除全局单例对窗口组件/回调的持有（泄漏修复 6b）。

        GatewayEngine 是全局单例，由**第一个**窗口的 backend 创建并注入
        get_model_config / tool_executor / agent_manager / session_store——
        其中 get_model_config 是窗口 backend 的 bound method，tool_executor /
        agent_manager 等也是窗口独有组件。若第一个窗口关闭而不清理，
        单例永久持有这些引用 → 第一个窗口对象树无法回收（T13 引用链 6b）。

        - 停止运行中的 worker（若在流式）
        - 清空待处理消息队列
        - 解除对窗口组件的持有（置 None）
        - 若自身仍是全局单例则置 None，允许下一个窗口重新创建
        """
        try:
            if self._conversation_executor.is_streaming:
                self._conversation_executor.cancel_streaming()
        except Exception:
            pass
        self._pending_queue.clear()
        self._get_model_config = None
        self._tool_executor = None
        self._agent_manager = None
        self._session_store = None
        if GatewayEngine._global_instance is self:
            GatewayEngine._global_instance = None
        # 标记引擎停用，让 backend 入口守卫能感知，避免对平台推送"引擎已停用"
        # 并让异步 future 永久挂起。
        self._is_active = False

    # ==================== BaseEngine 接口实现 ====================

    def get_current_session(self) -> Optional[ChatSession]:
        """获取当前 Gateway 会话"""
        return self._conversation_core.session_manager.get_current_session()

    # ==================== 会话管理 ====================

    def add_session(self, session: ChatSession) -> None:
        """添加 Gateway 会话并切换为当前会话"""
        sm = self._conversation_core.session_manager
        sm.sessions.append(session)
        sm.current_index = len(sm.sessions) - 1
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
            callbacks: 回调字典

        Returns:
            True 如果消息已被处理
        """
        callbacks = callbacks or {}

        if text.startswith("/"):
            response = self._handle_command(session, text)
            if response is not None:
                cb = callbacks.get("stream_finished")
                if cb:
                    cb(response)
                self._save_to_store(session)
                return True

        return self._send_to_ai(session, text, callbacks)

    # ==================== 命令处理 ====================

    def _handle_command(self, session: ChatSession, text: str) -> Optional[str]:
        """
        处理命令，返回响应文本或 None（未知命令）
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

        elif cmd == "stop":
            return self._cmd_stop()

        elif cmd == "status":
            return self._cmd_status(session)

        elif cmd == "compact":
            return self._cmd_compact(session)

        else:
            return None

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
- `/stop` — 中止当前正在生成的回复
- `/status` — 查看当前会话状态（模型/Agent/上下文用量）
- `/compact` — 压缩当前会话上下文（快速模式：剪枝大工具输出+截断）
- `/help` — 显示此帮助

--------
💡 Gateway 会话与桌面端完全隔离，互不影响。
   历史会话可在桌面端 UI 列表中查看。"""

    def _cmd_model(self, args: str, session: ChatSession) -> str:
        """处理 /model 命令"""
        cfg = Settings.get_instance()

        current_provider = cfg.llm_selected_model.value or ""
        saved_providers = cfg.llm_saved_providers.value or {}

        session_meta = session.metadata.get("model") or ""
        session_provider = session_meta.get("provider", "") if isinstance(session_meta, dict) else ""
        session_model = session_meta.get("model", "") if isinstance(session_meta, dict) else session_meta

        if not args:
            if not saved_providers:
                return "📋 **当前模型**: 暂无配置的服务商\n请在桌面端设置中添加服务商。"

            lines = ["📋 **可用模型**:\n"]
            for config_id, info in sorted(saved_providers.items()):
                models = info.get("模型列表", [])
                model_name = info.get("模型名称", "")
                # 显示配置名称（如果存在），否则显示服务商名称
                display_name = info.get("name", "") or info.get("provider_name", config_id)
                # 判断是否为当前服务商：比较配置 ID 或服务商名称
                is_current = (config_id == current_provider) or (info.get("provider_name") == current_provider)
                marker = " ◀ 当前服务商" if is_current else ""
                # 会话覆盖判断：比较配置 ID 或服务商名称
                is_session = (config_id == session_provider) or (info.get("provider_name") == session_provider)
                session_marker = " ⚡ 会话覆盖" if is_session else ""

                if models:
                    lines.append(f"**{display_name}**{marker}{session_marker}")
                    lines.append(f"  模型: `{model_name}`")
                    if len(models) > 1:
                        lines.append(f"  可选: {', '.join(f'`{m}`' for m in models[:8])}")
                else:
                    lines.append(f"**{display_name}**{marker}{session_marker} — `{model_name}`")

                if is_session:
                    lines.append(f"  ⚡ 会话级模型: `{session_model}`")

                lines.append("")

            return "\n".join(lines).strip()

        parts = args.strip().split(maxsplit=1)
        provider_name = parts[0]
        model_name = parts[1] if len(parts) > 1 else ""

        # 匹配服务商：支持配置 ID、服务商名称、配置名称
        matches = []
        for config_id, info in saved_providers.items():
            # 匹配配置 ID
            if provider_name.lower() in config_id.lower():
                matches.append(config_id)
                continue
            # 匹配服务商名称
            if info.get("provider_name") and provider_name.lower() in info["provider_name"].lower():
                matches.append(config_id)
                continue
            # 匹配配置名称
            if info.get("name") and provider_name.lower() in info["name"].lower():
                matches.append(config_id)
                continue

        if not matches:
            return f"❌ 未找到服务商 `{provider_name}`\n发送 `/model` 查看服务商列表。"

        config_id = matches[0]
        config = saved_providers[config_id]
        display_name = config.get("name", "") or config.get("provider_name", config_id)

        if not model_name:
            model_name = config.get("模型名称", "")
            available = config.get("模型列表", [])
            if available:
                opt_list = ", ".join(f"`{m}`" for m in available[:10])
                return f"📋 **{display_name}** 当前模型: `{model_name}`\n可选: {opt_list}"
            return f"📋 **{display_name}** 当前模型: `{model_name}`"

        session.metadata["model"] = {"provider": config_id, "model": model_name}
        return f"✅ 已切换到 **{display_name}** 的模型: `{model_name}`\n（仅当前 Gateway 会话生效）"

    def _cmd_agent(self, args: str, session: ChatSession) -> str:
        """处理 /agent 命令"""
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
            return f"✅ 当前会话已切换到 Agent: `{args}`\n（仅当前 Gateway 会话生效）"
        else:
            agents = self._agent_manager.list_primary_agents()
            matches = [a for a in agents if args.lower() in a.name.lower()]
            if matches:
                lines = [f"找到 {len(matches)} 个匹配:\n"]
                for m in matches[:10]:
                    lines.append(f"- **{m.name}**: {m.description}")
                return "\n".join(lines)
            return f"❌ 未找到 Agent `{args}`\n发送 `/agent` 查看可用列表。"

    def _cmd_stop(self) -> str:
        """处理 /stop 命令：中止当前正在生成的回复"""
        executor = self._conversation_executor
        if not executor.is_streaming:
            return "当前没有正在进行的回复。"

        executor.cancel_worker()
        queued = len(self._pending_queue)
        suffix = f"（另有 {queued} 条排队消息将继续处理）" if queued else ""
        return f"🛑 已停止当前回复{suffix}"

    def _cmd_status(self, session: ChatSession) -> str:
        """处理 /status 命令：当前会话状态总览"""
        cfg = Settings.get_instance()

        # 模型：会话级覆盖 > 全局当前
        session_meta = session.metadata.get("model")
        if isinstance(session_meta, dict) and session_meta.get("model"):
            model_line = f"`{session_meta['model']}`（会话覆盖）"
        else:
            model_line = f"`{cfg.llm_selected_model.value or '未配置'}`"

        agent = session.metadata.get("agent") or self._current_agent or "plan"
        streaming = "生成中…" if self._conversation_executor.is_streaming else "空闲"
        queued = len(self._pending_queue)

        lines = [
            "📊 **会话状态**",
            f"- 模型: {model_line}",
            f"- Agent: `{agent}`",
            f"- 消息数: {session.message_count}",
            f"- 状态: {streaming}" + (f"（排队 {queued} 条）" if queued else ""),
        ]

        # 上下文用量（估算，可能略有开销，失败静默跳过）
        try:
            compactor = self._conversation_core.compactor
            messages = session.get_context_messages()
            budget = compactor.get_budget(self._get_model_config() or {})
            usage = compactor.get_usage(messages, budget)
            used = usage.get("used_tokens", 0)
            percent = int(usage.get("percent", 0))
            lines.append(f"- 上下文: ~{used} / {budget} tokens（{percent}%）")
        except Exception as e:
            logger.debug(f"[GatewayEngine] /status usage skipped: {e}")

        return "\n".join(lines)

    def _cmd_compact(self, session: ChatSession) -> str:
        """处理 /compact 命令：压缩当前会话上下文

        快速模式（allow_llm_summary=False）：尾保留 + 大工具输出剪枝 + 截断，
        毫秒级完成不阻塞主线程。完整 LLM 摘要压缩请在桌面端执行。
        """
        executor = self._conversation_executor
        if executor.is_streaming:
            return "⏳ 正在生成回复，请先发送 `/stop` 再压缩。"

        messages = session.get_context_messages()
        if not messages:
            return "当前会话没有历史消息，无需压缩。"

        try:
            compactor = self._conversation_core.compactor
            llm_config = self._get_model_config() or {}
            budget = compactor.get_budget(llm_config)

            before_usage = compactor.get_usage(messages, budget)
            before_tokens = before_usage.get("used_tokens", 0)
            before_count = len(messages)

            # system 消息不参与压缩（与 chat_worker 迭代内压缩一致）
            system_message = None
            if messages and messages[0].get("role") == "system":
                system_message = messages.pop(0)

            compacted, state, cache = compactor.compact(
                messages,
                budget,
                existing_cache=getattr(session, "compaction_cache", None),
                allow_llm_summary=False,
            )

            if system_message is not None:
                compacted.insert(0, system_message)

            after_tokens = compactor.get_usage(compacted, budget).get("used_tokens", 0)
            if len(compacted) >= before_count:
                return f"✨ 上下文负载不高（~{before_tokens} / {budget} tokens），无需压缩。"

            session.set_messages(compacted, preserve_compaction=True)
            session.compaction_cache = cache
            self._save_to_store(session)

            saved_pct = int((1 - after_tokens / before_tokens) * 100) if before_tokens else 0
            return (
                f"✅ 上下文已压缩\n"
                f"- 消息: {before_count} → {len(compacted)} 条\n"
                f"- Token: ~{before_tokens} → ~{after_tokens}（节省 {saved_pct}%）"
            )
        except Exception as e:
            logger.error(f"[GatewayEngine] /compact failed: {e}", exc_info=True)
            return f"❌ 压缩失败: {str(e)[:150]}"

    def _cmd_session(self, args: str) -> str:
        """处理 /session 命令"""
        sm = self._conversation_core.session_manager
        if not args:
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

        candidates = [s for s in sm.get_all_sessions() if args in s.session_id or args in s.name]
        if len(candidates) == 1:
            self.switch_to_session(candidates[0].session_id)
            return f"✅ 已切换到会话: **{candidates[0].name}**"
        elif len(candidates) > 1:
            lines = ["找到多个匹配:\n"]
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
            self._pending_queue.append((session, text, callbacks))
            logger.info(f"[GatewayEngine] Message queued ({len(self._pending_queue)} pending)")
            return True

        # 添加用户消息
        session.add_user_message(content=text)

        # 引擎可能已被窗口关闭时 cleanup（多窗口共享全局单例，cleanup 会置 None），
        # 此时消息仍可能经 PlatformManager 残留回调路由过来 → 直接崩溃 None 调用。
        # 上层 ChatBackend._on_gateway_input 已在入口守卫拦住，这里是兜底。
        if self._get_model_config is None or not self._is_active:
            logger.warning("[GatewayEngine] skip message: engine cleaned up (window closed)")
            # 不再向平台推送错误：上层入口已处理提示，避免重复 / 吓用户。
            return False

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
            if isinstance(session_model_meta, dict):
                override_provider = session_model_meta.get("provider", "")
                override_model = session_model_meta.get("model", "")
                if override_provider and override_model:
                    llm_config = {**llm_config, "model": override_model}
                    saved_providers = Settings.get_instance().llm_saved_providers.value or {}
                    if override_provider in saved_providers:
                        provider_cfg = saved_providers[override_provider].copy()
                        provider_cfg.pop("备注", None)
                        provider_cfg.pop("获取地址", None)
                        provider_cfg.pop("模型列表", None)
                        provider_cfg["模型名称"] = override_model
                        llm_config = provider_cfg
            elif isinstance(session_model_meta, str) and session_model_meta:
                llm_config = {**llm_config, "model": session_model_meta}

        # 构建消息
        messages = self._build_messages(session, llm_config)

        # 获取工具
        tools = self._get_tools(session=session)

        # 包装 Gateway 回调
        wrapped_callbacks = self._make_gateway_callbacks(session, callbacks, llm_config)

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
        llm_config: Optional[Dict] = None,
    ) -> Dict[str, Callable]:
        """包装 Gateway 特有逻辑的回调"""
        cb_content = callbacks.get("content_received", _noop)
        cb_finished = callbacks.get("stream_finished", _noop)
        cb_error = callbacks.get("error", _noop)

        chunks = []

        def on_content(chunk: str):
            chunks.append(chunk)
            cb_content(chunk)

        def on_finished(response: str):
            final_response = response or "".join(chunks)
            current_msgs = session.get_context_messages()
            has_new_assistant = (
                current_msgs
                and current_msgs[-1].get("role") == "assistant"
                and self._is_recent_message(current_msgs[-1])
            )
            if not has_new_assistant and final_response.strip():
                model_name = str(llm_config.get("模型名称", "") or "") if llm_config else ""
                session.add_assistant_message(content=final_response, model_name=model_name or None)
            self._save_to_store(session)
            cb_finished(final_response)
            self.worker_finished.emit(final_response)
            self._process_next()

        def on_error(error: str):
            cb_error(error)
            self.worker_error.emit(error)
            self._process_next()

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

        system_prompt = ""
        if self._agent_manager:
            system_prompt = self._agent_manager.get_agent_system_prompt(agent_name)
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})

        gateway_constraints = r"""
## Gateway 模式约束
你正在通过 Gateway 对外提供 AI 服务（当前平台可能为钉钉/企业微信等）。
以下是必须遵守的规则：

### 【禁止使用的工具】
- ❌ **禁止** 使用 `question` 工具
- ❌ **禁止** 使用 `subagent_para` 和 `subagent_status` 工具
- ❌ **禁止** 使用 `todowrite` 工具

### 【文件引用规则 — 硬性强制】
- **禁止** 在任何回答中直接引用本地文件路径（如 `C:\xxx`、`/home/xxx`、`D:/work/xxx` 等）
- **必须** 先调用 `upload_file` 工具将文件上传为公开链接，然后在回答中使用 Markdown 格式引用该链接：
  - 图片 → `![描述](链接)`
  - 其他文件 → `📎 [文件名](链接)`
- ❌ 错误示例：`"查看文件 /tmp/report.pdf"` → **禁止**
- ✅ 正确示例：`"查看报告 📎 [report.pdf](https://gitee.com/...)"` → **必须这样**
- 此规则适用于所有文件类型（图片、PDF、文档、日志、代码文件等）

### 【其他必须遵守】
- 所有任务必须**一次性完成**
- 如果信息不足，使用 `websearch` 或 `webfetch` 自行搜索
- 直接输出最终结果
- 回答要简洁、完整、可直接使用
"""
        if system_prompt:
            messages[0]["content"] = messages[0]["content"] + "\n\n" + gateway_constraints.strip()
        else:
            messages.append({"role": "system", "content": gateway_constraints.strip()})

        messages.extend(session.get_context_messages())
        return messages

    def _get_tools(self, session: Optional[ChatSession] = None) -> List[Dict]:
        """获取工具 schema（过滤掉 Gateway 不适用的交互式工具）"""
        agent_name = None
        if self._agent_manager:
            s = session or self.get_current_session()
            if s:
                agent_name = s.metadata.get("agent") or self._current_agent

        if agent_name and self._agent_manager:
            tools = self._agent_manager.get_agent_tools_schema(
                agent_name,
                builtin_tools=self._tool_executor._builtin_tools if self._tool_executor else None,
            )
        else:
            tools = get_builtin_tools_schema(
                agent_manager=self._agent_manager,
                builtin_tools=self._tool_executor._builtin_tools if self._tool_executor else None,
                session_id=str(getattr(session, "session_id", "") or "") if session else "",
            )

        from app.core.conversation.config import PermissionStrategy, filter_interactive_tools

        return filter_interactive_tools(tools, PermissionStrategy.AGENT_CONFIG)

    # ==================== 持久化 ====================

    @staticmethod
    def _is_recent_message(msg: Dict, threshold_seconds: int = 3) -> bool:
        """检查消息是否是最近几秒内创建的"""
        from datetime import datetime

        ts = msg.get("timestamp", "")
        if not ts:
            return False
        try:
            msg_time = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            return (datetime.now() - msg_time).total_seconds() < threshold_seconds
        except ValueError, TypeError:
            return False

    def _save_to_store(self, session: ChatSession) -> None:
        """保存 Gateway 会话到 SQLite"""
        if not self._session_store:
            return
        try:
            self._session_store.save_session(session.to_dict())
        except Exception as e:
            logger.warning(f"[GatewayEngine] Failed to save session: {e}")
