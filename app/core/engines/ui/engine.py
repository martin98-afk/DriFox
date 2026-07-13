# -*- coding: utf-8 -*-
"""
UI 对话引擎 — 处理桌面 LLM 对话的核心逻辑

从 app/core/chat_engine.py 迁移而来，类名改为 UIEngine。
保留 ChatEngine 别名在 __init__.py 中提供向后兼容。
"""

import os
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from app.core.agent import PermissionResolver
from app.core.chat_session import (
    ChatSession,
    SessionManager,
)
from app.core.conversation.adapters import UIConversationAdapter
from app.core.conversation.config import ConversationConfig, PermissionStrategy
from app.core.conversation.core import ConversationCore
from app.core.engines.base import BaseEngine
from app.core.message_content import content_to_text
from app.core.token_estimator import count_messages_tokens, count_tools_tokens, get_model_token_ratio
from app.tools import get_builtin_tools_schema


class UIEngine(BaseEngine):
    """UI 对话引擎 — 负责组装上下文并驱动 worker。"""

    def __init__(
        self,
        session_manager: SessionManager,
        get_model_config: Callable[[], Dict[str, Any]],
        tool_executor: Optional[Any] = None,
        agent_manager: Any = None,
        get_chat_cards: Callable[[], List[Any]] = None,
        get_memory_context: Optional[Callable[[], str]] = None,
        worker_callbacks: Optional[Dict[str, Callable]] = None,
        api_mode: bool = False,
        backend: Any = None,
    ):
        self._session_manager = session_manager
        self._get_model_config = get_model_config
        self._tool_executor = tool_executor
        self._agent_manager = agent_manager
        self._get_chat_cards = get_chat_cards
        self._get_memory_context = get_memory_context
        self._backend = backend
        self._callbacks: Dict[str, Callable] = {}
        self._current_agent: Optional[str] = "build"

        # API 模式专用：直接回调（绕过 Qt 信号-槽，避免跨线程事件循环问题）
        self._worker_callbacks = worker_callbacks or {}
        self._api_mode = api_mode

        # ===== ConversationCore（聚合 Compactor + PermissionCache + ContextBudgetAllocator）=====
        self._conversation_core = ConversationCore.create(
            get_model_config=get_model_config,
            agent_manager=agent_manager,
            backend=backend,
            session_manager=self._session_manager,
        )

        # ===== ConversationExecutor（统一 Worker 执行）=====
        from app.core.conversation.executor import ConversationExecutor

        config = ConversationConfig(
            permission_strategy=PermissionStrategy.INTERACTIVE,
            interactive_check_callback=self._check_tool_permission,
        )
        self._conversation_executor = ConversationExecutor(
            core=self._conversation_core,
            config=config,
            tool_executor=tool_executor,
            agent_manager=agent_manager,
        )

        # ===== UIConversationAdapter（Qt 信号转发）=====
        self._adapter = UIConversationAdapter(
            core=self._conversation_core,
            executor=self._conversation_executor,
        )

        # ===== Adapter 信号 → UIEngine 回调 =====
        self._adapter.content_received.connect(lambda p: self._emit("content_received", p))
        self._adapter.reasoning_content_received.connect(lambda p: self._emit("reasoning_content_received", p))
        self._adapter.thinking_started.connect(lambda: self._emit("thinking_started"))
        self._adapter.tool_call_started.connect(lambda i, n, a, r: self._emit("tool_call_started", i, n, a, r))
        self._adapter.tool_args_updated.connect(lambda i, n, p: self._emit("tool_args_updated", i, n, p))
        self._adapter.tool_result_received.connect(lambda i, n, a, r: self._emit("tool_result_received", i, n, a, r))
        self._adapter.question_asked.connect(lambda i, q, e: self._emit("question_asked", i, q, e))
        self._adapter.permission_approval_requested.connect(
            lambda i, n, a: self._emit("permission_approval_requested", i, n, a)
        )
        self._adapter.stream_finished.connect(lambda r: self._on_worker_finished(r))
        self._adapter.messages_updated.connect(lambda ms: self._emit("messages_updated", ms))
        self._adapter.error_occurred.connect(lambda e: self._on_error(e))
        self._adapter.retry_status.connect(lambda *a: self._emit("retry_status", *a))
        self._adapter.retry_resolved.connect(lambda: self._emit("retry_resolved"))
        self._adapter.stream_started.connect(lambda: self._emit("stream_started"))
        self._adapter.context_updated.connect(lambda tc, lim, fa=False: self._emit("context_updated", tc, lim, fa))

        # 调用父类构造
        super().__init__(self._conversation_core, self._conversation_executor)

    # ========== 向后兼容属性 ==========

    @property
    def compactor(self):
        """暴露压缩器供外部使用（如工具迭代中压缩）"""
        return self._conversation_core.compactor

    @property
    def _permission_cache(self):
        """向后兼容：权限缓存已移至 ConversationCore"""
        return self._conversation_core.permission_cache

    @property
    def _compactor(self):
        """向后兼容：压缩器已移至 ConversationCore"""
        return self._conversation_core.compactor

    @property
    def _current_worker(self):
        """向后兼容：Worker 已由 ConversationExecutor 管理"""
        return getattr(self._conversation_executor, "_current_worker", None)

    # ========== 属性访问（正式接口）==========

    @property
    def current_agent(self) -> str:
        """获取当前 Agent"""
        return self._current_agent or "build"

    def set_current_agent(self, value: str):
        """设置当前 Agent"""
        self._current_agent = value

    @property
    def session_manager(self) -> SessionManager:
        return self._session_manager

    def set_streaming(self, value: bool):
        """设置流式状态（已由 ConversationExecutor 管理，忽略手动设置）"""
        pass

    def get_context_usage(self) -> tuple:
        """获取上下文使用情况（用于兼容性）"""
        return (0, 0)

    def get_current_session(self):
        """实现 BaseEngine 接口 — 获取当前会话"""
        return self._session_manager.get_current_session()

    # ========== Agent 管理 ==========

    def _get_agent_manager(self):
        return self._agent_manager

    def set_session_manager(self, session_manager):
        """Update the session manager reference (used when session is archived)."""
        self._session_manager = session_manager
        # 🔧 修复：同步 ConversationCore 的 session_manager 引用
        # 否则 ConversationExecutor.execute() 会通过 ConversationCore 读到旧的 session_manager
        if hasattr(self, "_conversation_core"):
            self._conversation_core.session_manager = session_manager

    # ========== 权限检查 ==========

    def _check_tool_permission(self, tool_name: str, arguments: dict) -> str:
        # ========== 工具开关过滤（优先于 Agent 权限检查） ==========
        # 前移至此以接入 PermissionStrategy.INTERACTIVE 的 ask/deny 对话框机制。
        # 此前在 ToolExecutor 中检查，"ask" 只能返回错误文本，无法弹出确认对话框。
        # 数据源：per-window controller (多窗口隔离) → 兜底：全局 Settings
        check_name = tool_name
        if tool_name.startswith("mcp__"):
            parts = tool_name.split("__", 2)
            check_name = parts[2] if len(parts) > 2 else tool_name

        controller = None
        if self._backend is not None:
            controller = getattr(self._backend, "tool_permission_controller", None)

        if controller is not None:
            toggles = controller.get_toggles()
            behavior = controller.get_behavior()
        else:
            # 兜底：全局 Settings（API 模式等无 controller 的场景）
            from app.utils.config import Settings

            settings = Settings.get_instance()
            toggles = dict(settings.tool_toggles.value)
            behavior = settings.tool_off_behavior.value

        is_enabled = toggles.get(check_name, True)
        if not is_enabled:
            logger.info(f"[ToolToggle] tool={tool_name} check_name={check_name} enabled=False behavior={behavior}")
            return behavior  # "deny" 或 "ask"，由 ConversationExecutor 的 INTERACTIVE 策略驱动对话框
        # ========== 工具开关过滤结束 ==========

        agent_manager = self._get_agent_manager()
        if not agent_manager or not self._current_agent:
            return "allow"

        try:
            agent = agent_manager.get_agent(self._current_agent)
            if not agent:
                logger.warning(f"[_check_tool_permission] Agent not found: {self._current_agent}")
                return "allow"

            perm_resolver = PermissionResolver(agent.permission, {}, agent.tools)
            result = perm_resolver.resolve(tool_name)
            logger.info(f"[_check_tool_permission] agent={self._current_agent}, tool={tool_name}, result={result}")

            if tool_name == "bash":
                command = arguments.get("command", "")
                return perm_resolver.resolve(tool_name, command)
            elif tool_name in ("read", "edit", "multi_edit", "write"):
                file_path = arguments.get("filePath", "")
                return perm_resolver.resolve(tool_name, file_path)
            elif tool_name == "webfetch":
                url = arguments.get("url", "")
                return perm_resolver.resolve(tool_name, url)
            elif tool_name == "websearch":
                query = arguments.get("query", "")
                return perm_resolver.resolve(tool_name, query)
            elif tool_name == "subagent_para":
                tasks = arguments.get("tasks", [])
                if tasks and len(tasks) > 0:
                    first_agent = tasks[0].get("agent", "")
                    return perm_resolver.resolve_task(first_agent)
                return perm_resolver.resolve_task("")
            elif tool_name == "skill":
                skill_name = arguments.get("name", "")
                return perm_resolver.resolve(tool_name, skill_name)
            else:
                return perm_resolver.resolve(tool_name)

        except Exception as e:
            logger.warning(f"[ChatEngine] Permission check error: {e}")
            return "allow"

    def _on_permission_approval_requested(self, tool_call_id: str, tool_name: str, arguments: dict):
        self._emit("permission_approval_requested", tool_call_id, tool_name, arguments)

    def approve_tool_permission(self, tool_call_id: str, auto_allow: bool = False, session_allow: bool = False):
        """批准工具权限请求（转发给 Worker）"""
        worker = getattr(self._conversation_executor, "_current_worker", None)
        if worker:
            worker.approve_permission(tool_call_id, auto_allow, session_allow)

    def deny_tool_permission(self, tool_call_id: str):
        worker = getattr(self._conversation_executor, "_current_worker", None)
        if worker:
            worker.deny_permission(tool_call_id)

    def clear_session_permission_cache(self, tool_name: str = None):
        """清除会话级权限缓存"""
        cache = self._conversation_core.permission_cache
        if tool_name:
            cache.deny(tool_name)
        else:
            cache.clear_session()

    # ========== 回调管理 ==========

    def set_callback(self, event: str, callback: Callable):
        self._callbacks[event] = callback

    def clear_callbacks(self):
        """清除所有 UI 回调，防止异步回调访问已销毁的 widget"""
        self._callbacks.clear()

    def _emit(self, event: str, *args, **kwargs):
        # API 模式优先使用 _worker_callbacks
        callback = self._callbacks.get(event)
        if not callback and self._api_mode:
            callback = self._worker_callbacks.get(event)

        if callback:
            callback(*args, **kwargs)

    # ========== Agent 切换 ==========

    def switch_agent(self, agent_name: Optional[str]):
        agent_manager = self._get_agent_manager()
        if agent_name is None or agent_name.lower() in ("default", "通用"):
            self._current_agent = "build"
            self._invalidate_session_system_prompt_cache()
            logger.info("[ChatEngine] Switched to default agent: build")
            self._emit("agent_switched", "build")
            return

        agent = agent_manager.get_agent(agent_name)
        if not agent:
            logger.warning(f"[ChatEngine] Agent not found: {agent_name}")
            return

        self._current_agent = agent_name
        # 🔧 修复：切换智能体时必须清空当前 session 的 system_prompt 缓存，
        # 否则下次 build_messages 会复用旧 agent 的 prompt（包括 BuildSystemPrompt hook 注入的身份定义）
        self._invalidate_session_system_prompt_cache()
        logger.info(f"[ChatEngine] Switched to agent: {agent_name}")
        self._emit("agent_switched", agent_name)

    def _invalidate_session_system_prompt_cache(self):
        """清空当前 session 的 system_prompt 缓存，强制下次重建

        当智能体切换、agent 配置变更、hook 配置变更时调用，
        避免 BuildSystemPrompt hook 注入的旧 agent 身份定义残留。
        """
        try:
            session = self._session_manager.get_current_session()
            if session is None:
                return
            session.system_prompt = ""
            if hasattr(session, "_system_prompt_agent"):
                session._system_prompt_agent = ""
            logger.debug(
                f"[ChatEngine] Invalidated system_prompt cache for session {session.session_id}"
            )
        except Exception as e:
            logger.warning(f"[ChatEngine] Failed to invalidate system_prompt cache: {e}")

    # ========== 消息发送 ==========

    def send_message(self, user_text: str, *args, **kwargs) -> bool:
        if self._conversation_executor.is_streaming:
            logger.warning("[ChatEngine] Already streaming, ignoring new message")
            return False

        session = self._session_manager.get_current_session()
        if not session:
            logger.error("[ChatEngine] No current session")
            return False

        llm_config = self._get_model_config()
        if not llm_config:
            logger.error("[ChatEngine] No LLM config available")
            self._emit("error", "配置无效，请检查模型设置")
            return False

        # ---- 提取多模态内容（含图片的 _user_content），用于 session 存储和 LLM 消息构建 ----
        _user_content = kwargs.pop("_user_content", None)
        content_to_store = _user_content or user_text
        # user_text 始终是纯文本版本，用于 hook 触发和 UI 显示

        # 公共辅助方法：同步触发 hook 并收集输出
        # 多窗口隔离：使用当前窗口的工作目录，不依赖进程级 os.getcwd()
        _window_workdir = self._backend.tool_executor.get_workdir() if self._backend and self._backend.tool_executor else None
        if not _window_workdir:
            _window_workdir = os.getcwd()

        def _trigger_and_inject(hook_mgr, event_name, extra_context=None, msg_text=None, inject_to_session=None):
            """同步触发 hook，收集输出并注入 session.messages（只追加不删除）"""
            if extra_context is None:
                extra_context = {}
            ctx = {
                "project_root": _window_workdir,
                # 【新增】让 hook 能识别当前执行角色（与 subagent_worker._build_hook_context 对齐）
                "current_role": "primary",
                "is_subagent_call": False,
            }
            ctx.update(extra_context)
            results = hook_mgr.trigger_event(
                event_name,
                context=ctx,
                current_message=msg_text or user_text,
                trigger_async=False,  # 关键：同步执行，确保输出在返回值中
            )
            # 收集成功执行的 hook 输出，注入 session
            if inject_to_session:
                from app.core.backend import _inject_hook_to_session

                for r in results:
                    if r.success and r.output:
                        _inject_hook_to_session(inject_to_session, event_name, r.output, r.status_message)

        # 获取 session_id（用于 hook context；必须先于 hook 触发块赋值，
        # 否则 L365/L378 会在 Python 编译期被识别为"先读后写"的局部变量，触发 UnboundLocalError）
        _session_id = session.session_id if session else ""

        # 多窗口隔离：始终使用当前窗口 Backend 的 HookManager
        hook_mgr = getattr(self._backend, "hook_manager", None) if self._backend else None

        if hook_mgr:
            # UserPromptSubmit: 最先触发，用户刚提交原始 prompt
            _trigger_and_inject(
                hook_mgr,
                "UserPromptSubmit",
                {
                    "message": user_text,
                    "session_id": _session_id,
                },
                inject_to_session=session,
            )
            # PreUserMessage: 注入条目记忆 + 关键文档 + worktree 上下文
            memory_ctx = {}
            worktree_ctx = {}
            try:
                if self._backend:
                    memory_ctx = self._backend.build_memory_context_dict() or {}
                    worktree_ctx = self._backend._build_worktree_context_dict() or {}
            except Exception:
                pass
            # 🆕 读取 main_widget 存下的 pending_command/pending_skill，
            # 传递给 PreUserMessage hook 进行注入
            pending_cmd = session.metadata.pop("_pending_command", None) if session else None
            pending_skill = session.metadata.pop("_pending_skill", None) if session else None

            pre_user_ctx = {
                "message": user_text,
                "session_id": _session_id,
                **memory_ctx,
                **worktree_ctx,
            }
            if pending_cmd:
                pre_user_ctx["pending_command"] = pending_cmd
            if pending_skill:
                pre_user_ctx["pending_skill"] = pending_skill
            _trigger_and_inject(hook_mgr, "PreUserMessage", pre_user_ctx, inject_to_session=session)

        session.add_user_message(content=content_to_store)

        if hook_mgr:
            post_user_ctx = {
                "message": user_text,
                "session_id": _session_id,
            }
            # 补充多模态内容（如有图片）
            if _user_content is not None and _user_content != user_text:
                post_user_ctx["user_content"] = _user_content
            _trigger_and_inject(hook_mgr, "PostUserMessage", post_user_ctx, inject_to_session=session)

            # 通知 UI 刷新（预对话 hook 已注入 session.messages）
            if self._backend:
                self._backend._hook_messages_updated.emit()

        messages = self._adapter.build_messages(
            self._session_manager.get_current_session(),
            llm_config,
            current_agent=self._current_agent,
        )
        if self._current_agent:
            available_tools = self._get_agent_manager().get_agent_tools_schema(self._current_agent)
        else:
            available_tools = get_builtin_tools_schema(
                self._get_agent_manager(),
                builtin_tools=self._tool_executor._builtin_tools if self._tool_executor else None,
            )

        # 使用 ConversationExecutor 执行
        callbacks = self._adapter.get_callbacks()
        success = self._conversation_executor.execute(
            messages=messages,
            llm_config=llm_config,
            tools=available_tools,
            callbacks=callbacks,
        )
        return success

    # ========== 消息构建 ==========

    def _build_messages(
        self,
        session: ChatSession,
        llm_config: Dict,
        allow_llm_summary: bool = False,
    ) -> List[Dict]:
        """委托给 ContextBudgetAllocator 构建消息"""
        messages = self._conversation_core.context_builder.build_messages(
            session=session,
            llm_config=llm_config,
            allow_llm_summary=allow_llm_summary,
            current_agent=self._current_agent,
        )

        return messages

    def get_context_usage_snapshot(
        self,
        session: Optional[ChatSession] = None,
        llm_config: Optional[Dict] = None,
        api_prompt_tokens: int = 0,
        api_message_count: int = 0,
        from_api: bool = False,
    ) -> Dict[str, int]:
        session = session or self._session_manager.get_current_session()
        llm_config = llm_config or self._get_model_config()
        if not session or not llm_config:
            return {
                "used_tokens": 0,
                "budget_tokens": 0,
                "percent": 0,
                "compaction": self._conversation_core.compactor._make_state(),
                "normal_tokens": 0,
                "compacted_tokens": 0,
                "breakdown": [],
            }

        # 快速路径：直接用 session.messages + system prompt 算 token
        # 不走 _build_messages → build_messages → compactor.compact 全流程，
        # 避免在主线程上阻塞 UI 事件循环（anyio.run(to_thread.run_sync, compactor.compact)）。
        # session.system_prompt 已在 context_builder.build_messages 中缓存，
        # session.messages 由 _on_messages_updated → set_messages 更新，两者都是现成的。
        system_prompt = getattr(session, "system_prompt", "") or ""
        # 若 session 尚未缓存 system prompt（如刚创建、尚未 build_messages），
        # 主动从 agent_manager 取当前 agent 的 system prompt，确保「系统提示」类别
        # 始终被统计并显示，避免系统提示既不出现在 breakdown 也不计入总量。
        if not system_prompt:
            try:
                am = self._get_agent_manager()
                if am:
                    # 构建 extra_context，与 context_builder.build_messages 行为一致
                    # （否则 ProjectNotesHook 等依赖 project_root/project_name 的 hook 会拿到空值）
                    extra_context: Dict[str, Any] = {}
                    try:
                        if self._tool_executor and hasattr(self._tool_executor, "get_workdir"):
                            extra_context["project_root"] = self._tool_executor.get_workdir() or ""
                        if self._backend and hasattr(self._backend, "current_project"):
                            extra_context["project_name"] = self._backend.current_project or ""
                    except Exception:
                        pass
                    system_prompt = am.get_agent_system_prompt(
                        self._current_agent, is_subagent_call=False,
                        extra_context=extra_context,
                    ) or ""
                    # 缓存回 session，与 context_builder.build_messages 行为一致
                    if system_prompt and not getattr(session, "system_prompt", ""):
                        try:
                            session.system_prompt = system_prompt
                        except Exception:
                            pass
            except Exception:
                system_prompt = ""
        model = str(llm_config.get("模型名称", "gpt-4o") or "gpt-4o")

        approx_messages: List[Dict] = []
        if system_prompt:
            approx_messages.append({"role": "system", "content": system_prompt})
        approx_messages.extend(session.messages)

        # 获取工具 schema（与实际 API 请求一致），必须计入上下文占用
        # ⚠️ 旧实现此处漏传 tools，导致工具定义（35+ 工具）的 token 完全未计入，
        # 是本地估算与 API 返回的 prompt_tokens 差异巨大的主因之一。
        if self._current_agent:
            available_tools = self._get_agent_manager().get_agent_tools_schema(
                self._current_agent
            )
        else:
            available_tools = get_builtin_tools_schema(
                self._get_agent_manager(),
                builtin_tools=self._tool_executor._builtin_tools if self._tool_executor else None,
            )

        budget_tokens = max(1, self._conversation_core.context_builder.get_context_budget(llm_config))
        # 估算总量：系统提示 + 全部消息 + 工具定义（含模型分词校正系数）
        est_total = count_messages_tokens(approx_messages, model, tools=available_tools)

        # ---- 各类型上下文占比（按角色拆分，用于 WorkBuddy 风格占比条）----
        system_tokens = (
            count_messages_tokens([{"role": "system", "content": system_prompt}], model)
            if system_prompt
            else 0
        )
        tools_tokens = int(count_tools_tokens(available_tools, model) * get_model_token_ratio(model)) if available_tools else 0

        # 按消息角色拆分：用户消息 / 助手消息 / 工具结果 / Hook 注入
        # 每条消息独立计 token，且不含工具 schema 开销（工具定义单独计在 tools_tokens），
        # 避免与下面的 工具定义 重复计入。
        # 带 _hook_event 标记的消息是 hook 注入的动态上下文（如长期记忆、系统时间等），
        # 独立统计以便用户直观了解 hook 机制对上下文的占用。
        user_tokens = assistant_tokens = tool_tokens = hook_tokens = 0
        for msg in session.messages:
            role = msg.get("role", "")
            t = count_messages_tokens([msg], model)  # 不含 tools
            # 分离 hook 注入消息（带 _hook_event 标记），独立统计
            if msg.get("_hook_event"):
                hook_tokens += t
            elif role == "user":
                user_tokens += t
            elif role == "assistant":
                assistant_tokens += t
            elif role == "tool":
                tool_tokens += t
            else:
                # 其它角色（如内联 system 消息）兜底归入用户侧
                user_tokens += t

        breakdown = [
            {"key": "tools", "label": "工具定义", "tokens": tools_tokens, "color": "#f472b6"},
            {"key": "system", "label": "系统提示", "tokens": system_tokens, "color": "#5aa9ff"},
            {"key": "user", "label": "用户消息", "tokens": user_tokens, "color": "#34d399"},
            {"key": "hook", "label": "Hook 注入", "tokens": hook_tokens, "color": "#fb923c"},
            {"key": "assistant", "label": "助手消息", "tokens": assistant_tokens, "color": "#fbbf24"},
            {"key": "tool", "label": "工具结果", "tokens": tool_tokens, "color": "#a78bfa"},
        ]
        breakdown = [b for b in breakdown if b["tokens"] > 0]

        # ---- 确定上下文占用量 ----
        # 优先使用 API 返回的精确 prompt_tokens 作为权威值；
        # 若有 message_count 且新增了消息，估算增量：API值 + 新增消息估算
        # 若无 API 返回值（冷启动、无活跃对话），回退到本地估算。
        # ⚠️ from_api=False 表示 api_prompt_tokens 来自本地估算
        #   （count_messages_tokens），其覆盖口径（built_messages 的压缩子集）
        #   与 api_message_count（len(session.messages) 全量）不匹配，
        #   此时 delta 增量路径会导致严重低估甚至越来越小，直接跳过。
        if api_prompt_tokens > 0 and from_api:
            current_msg_count = len(session.messages)
            if api_message_count > 0 and current_msg_count > api_message_count:
                # 上次 API 调用后新增了消息：API 精确值 + 新增消息估算
                new_msgs = session.messages[api_message_count:]
                delta = count_messages_tokens(new_msgs, model)
                used_tokens = api_prompt_tokens + delta
            else:
                used_tokens = api_prompt_tokens
            # 按最终总量与本地估算的比例，等比缩放 breakdown 分量，
            # 保持视觉占比关系不变，总量锚定最终值。
            if est_total > 0:
                scale = used_tokens / est_total
                for b in breakdown:
                    b["tokens"] = max(1, int(b["tokens"] * scale))
        else:
            used_tokens = est_total

        percent = max(0, min(100, int((used_tokens / budget_tokens) * 100)))

        # 计算普通上下文和压缩上下文的 token 分解（供圆环绘制压缩段）
        compaction = dict(getattr(session, "compaction_state", {}) or {})
        normal_tokens = used_tokens
        compacted_tokens = 0

        if compaction.get("active"):
            compaction_cache = getattr(session, "compaction_cache", {}) or {}
            summary_msg = compaction_cache.get("summary_message")
            if summary_msg:
                compacted_tokens = count_messages_tokens([summary_msg], model)
                normal_tokens = used_tokens - compacted_tokens
            else:
                summarized_count = compaction.get("summarized_count", 0)
                kept_count = compaction.get("kept_count", 0)
                total_count = summarized_count + kept_count
                if total_count > 0:
                    compacted_tokens = int(used_tokens * summarized_count / total_count * 0.3)
                    normal_tokens = used_tokens - compacted_tokens

        return {
            "used_tokens": used_tokens,
            "budget_tokens": budget_tokens,
            "percent": percent,
            "compaction": compaction,
            "normal_tokens": normal_tokens,
            "compacted_tokens": compacted_tokens,
            "breakdown": breakdown,
        }

    def _start_worker(
        self,
        messages: List[Dict],
        llm_config: Dict,
        tools: List[Dict],
    ):
        """启动 Worker（委托 ConversationExecutor + UIConversationAdapter）"""
        callbacks = self._adapter.get_callbacks()
        success = self._conversation_executor.execute(
            messages=messages,
            llm_config=llm_config,
            tools=tools,
            callbacks=callbacks,
        )
        if success and not self._api_mode:
            self._emit("stream_started")

    def _on_worker_finished(self, response: str):
        # 🛡️ 防御性检查：如果 Executor 已不在流式状态（stop() 已调用或被新 worker 覆盖），
        # 忽略此残留信号（防止 RC3：Qt 事件队列中残留的 finished_with_content 触发 UI 更新）
        if not self._conversation_executor.is_streaming:
            logger.debug("[UIEngine] Ignoring stale stream_finished (stream already stopped)")
            return

        self._emit("stream_finished", response)

        # ⚠️ 注意：不在此处触发 PostAssistantMessage hook
        # chat_worker 内部已经在 _process_iteration 中通过 _trigger_worker_hook()
        # 触发了 PostAssistantMessage（同步模式），且在 engine 层面再次触发会导致
        # 所有 PostAssistantMessage hook 被调用两次（如语音播报播两遍）。
        # Worker 内部触发也会将 hook 输出注入到消息流，而 engine 层面不会，
        # 因此保留 worker 的触发即可覆盖所有场景（含错误路径由 chat_worker 的 Stop hook 处理）。

        # 保存缓存统计（在 worker 被清理前）
        self._save_cache_stats()
        # 对话结束后清理 worker，释放内存
        self.cleanup()

    def _save_cache_stats(self):
        """保存 Worker 的缓存统计到 Backend（Worker 被清理后仍可访问）"""
        from loguru import logger

        worker = getattr(self._conversation_executor, "_current_worker", None)
        if not worker:
            logger.debug("[_save_cache_stats] No worker found")
            return
        try:
            if hasattr(worker, "get_cache_stats"):
                stats = worker.get_cache_stats()
                if stats:
                    # stats 可能是 dict 或带 to_dict() 的对象
                    if isinstance(stats, dict):
                        stats_dict = stats
                    else:
                        stats_dict = stats.to_dict()
                    hit_rate = stats_dict.get("hit_rate", 0.0)
                    cache_read = stats_dict.get("cache_read_tokens", 0)
                    logger.debug(f"[_save_cache_stats] hit_rate={hit_rate}, read={cache_read}")
                    self._backend.set_last_cache_stats(stats_dict)
                else:
                    logger.debug("[_save_cache_stats] stats is None")
            else:
                logger.debug("[_save_cache_stats] worker has no get_cache_stats")
        except Exception as e:
            logger.debug(f"[_save_cache_stats] Error: {e}")

    def _trigger_post_assistant_message(self, response_or_error: str, is_error: bool = False):
        """统一触发 PostAssistantMessage hook"""
        # 多窗口隔离：使用当前窗口 Backend 的 HookManager
        hook_mgr = getattr(self._backend, "hook_manager", None) if self._backend else None
        if not hook_mgr:
            return

        session = self._session_manager.get_current_session()
        last_user_msg = ""
        session_id = ""
        if session:
            session_id = session.session_id or ""
            if hasattr(session, "messages"):
                for msg in reversed(session.messages):
                    if msg.get("role") == "user":
                        last_user_msg = content_to_text(msg.get("content", ""))
                        break

        # 多窗口隔离：使用当前窗口的工作目录
        project_root = self._backend.tool_executor.get_workdir() if self._backend and self._backend.tool_executor else None
        if not project_root:
            project_root = os.getcwd()
        context = {
            "project_root": project_root,
            "session_id": session_id,  # Claude Code 兼容字段
            # 【新增】让 hook 能识别当前执行角色（与 subagent_worker._build_hook_context 对齐）
            "current_role": "primary",
            "is_subagent_call": False,
        }
        if is_error:
            context["error"] = response_or_error
        else:
            # DriFoxx 自有格式（向后兼容）
            context["response"] = response_or_error
            # Claude Code 兼容格式
            context["assistant_response"] = response_or_error

        hook_mgr.trigger_event(
            "PostAssistantMessage",
            context=context,
            current_message=last_user_msg,
        )

    def _on_error(self, error: str):
        # 错误路径也触发 PostAssistantMessage（让 hook 能感知失败）
        self._trigger_post_assistant_message(error, is_error=True)
        self._emit("error", error)

    def _on_retry_status(self, error_type: str, attempt: int, max_retries: int, wait_time: float):
        """API 重试状态通知"""
        self._emit("retry_status", error_type, attempt, max_retries, wait_time)

    def provide_question_answer(self, answer: str):
        worker = getattr(self._conversation_executor, "_current_worker", None)
        if worker and hasattr(worker, "provide_answer"):
            worker.provide_answer(answer)

    def cleanup_worker(self):
        """清理当前 Worker 资源（用于切换会话前清理）

        由 Backend.cleanup_worker() 调用，委托 ConversationExecutor.cleanup() 实现。
        """
        self.cleanup()

    def get_current_worker(self):
        """获取当前 Worker 实例（供外部获取缓存统计等）"""
        return getattr(self._conversation_executor, "_current_worker", None)
