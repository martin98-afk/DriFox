# -*- coding: utf-8 -*-
"""
UI 对话引擎 — 处理桌面 LLM 对话的核心逻辑

从 app/core/chat_engine.py 迁移而来，类名改为 UIEngine。
保留 ChatEngine 别名在 __init__.py 中提供向后兼容。
"""

import os
import threading
from typing import Any, Callable, Dict, List, Optional

from loguru import logger
from PyQt5.QtCore import QEventLoop, QThread

from app.core.agent import PermissionResolver
from app.core.chat_session import (
    ChatSession,
    SessionManager,
)
from app.core.context_builder import TOOL_RESULT_MAX_LEN, prune_tool_result
from app.core.conversation.adapters import UIConversationAdapter
from app.core.conversation.config import ConversationConfig, PermissionStrategy
from app.core.conversation.core import ConversationCore
from app.core.engines.base import BaseEngine
from app.core.message_content import content_to_text
from app.core.token_estimator import count_tools_tokens, get_model_token_ratio, per_message_tokens
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

        # ===== 工具 schema token 缓存：避免 get_context_usage_snapshot 频繁触发 get_builtin_tools_schema =====
        # tools/init.py 虽有 5 秒 TTL，但工具执行间隔常超 5 秒（6~11s），缓存频繁失效。
        # 此处加一层 30 秒独立缓存，get_context_usage_snapshot 直接复用 tools 列表和已算好的 tools_tokens。
        self._tools_schema_cache: Dict[str, Any] = {
            "timestamp": 0.0,
            "tools": None,
            "tokens": 0,
        }

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
        # ========== 团队工具无条件放行 ==========
        # 团队工具在 schema 层已按 is_in_team 过滤（get_agent_tools_schema），
        # 仅团队成员可见。因此到执行层的工具调用必然来自团队成员，
        # 无需再经过工具开关和 Agent 权限检查，直接放行。
        _TEAM_TOOLS = {"team_send_message", "team_list_members"}
        if tool_name in _TEAM_TOOLS:
            return "allow"
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
        # ★ T28：UI 显式开启（用户调整过该工具）→ UI 为准，放行（跳过模板 deny）
        # 与子智能体 _check_ui_tool_permission 语义一致："UI 覆盖模板"
        if controller is not None and controller.is_user_modified(check_name):
            logger.debug(f"[ToolToggle] tool={tool_name} 用户显式开启，放行（覆盖模板）")
            return "allow"
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
            logger.debug(f"[ChatEngine] Invalidated system_prompt cache for session {session.session_id}")
        except Exception as e:
            logger.warning(f"[ChatEngine] Failed to invalidate system_prompt cache: {e}")

    # ========== 消息发送 ==========

    def send_message(self, user_text: str, *args, **kwargs) -> bool:
        """发送用户消息（非阻塞：hooks + build_messages 在后台线程执行，主线程处理 UI 事件）

        架构：主线程快速验证 → 启动 PreSendWorker(QThread) → QEventLoop 等待（处理 UI）
        → 主线程继续 executor.execute()。
        """
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

        # ---- 提取 hook_event 标记（团队任务邮件等），写入 session 消息时打标 ----
        hook_event = kwargs.pop("_hook_event", None)

        # ---- 主线程准备 context（无 I/O，纯数据组装） ----
        _window_workdir = (
            self._backend.tool_executor.get_workdir() if self._backend and self._backend.tool_executor else None
        )
        if not _window_workdir:
            _window_workdir = os.getcwd()

        _session_id = session.session_id if session else ""
        hook_mgr = getattr(self._backend, "hook_manager", None) if self._backend else None

        # 预构建 hook context dicts（主线程，快速；实际 hook 执行在 worker 线程）
        user_prompt_ctx = {"message": user_text, "session_id": _session_id} if hook_mgr else None

        pre_user_ctx = None
        if hook_mgr:
            memory_ctx = {}
            worktree_ctx = {}
            try:
                if self._backend:
                    memory_ctx = self._backend.build_memory_context_dict() or {}
                    worktree_ctx = self._backend._build_worktree_context_dict() or {}
            except Exception:
                pass
            # ⚠️ metadata.pop 必须在主线程（避免与 worker 线程竞态）
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

        post_user_ctx = {"message": user_text, "session_id": _session_id} if hook_mgr else None
        if post_user_ctx and _user_content is not None and _user_content != user_text:
            post_user_ctx["user_content"] = _user_content

        # ---- 启动后台 Worker：hooks + build_messages ----
        worker = _PreSendWorker(
            hook_mgr=hook_mgr,
            session=session,
            user_text=user_text,
            content_to_store=content_to_store,
            user_prompt_ctx=user_prompt_ctx,
            pre_user_ctx=pre_user_ctx,
            post_user_ctx=post_user_ctx,
            llm_config=llm_config,
            adapter=self._adapter,
            current_agent=self._current_agent,
            window_workdir=_window_workdir,
            agent_manager=self._get_agent_manager(),
            tool_executor=self._tool_executor,
            hook_event=hook_event,
        )
        worker.start()

        # ---- QEventLoop 等待：主线程不阻塞，可处理 UI 事件 ----
        loop = QEventLoop()
        worker.finished.connect(loop.quit)
        loop.exec()  # 处理 UI 事件直到 worker 完成

        # T6-A: 释放 _PreSendWorker 的 QThread C++ 对象（loop.exec 返回时线程已结束）。
        # 统一放在这里，正常路径与下方 _error 提前 return 分支都覆盖。
        try:
            worker.deleteLater()
        except RuntimeError:
            pass

        if worker._error:
            logger.error(f"[ChatEngine] PreSendWorker failed: {worker._error}")
            self._emit("error", f"消息预处理失败: {worker._error}")
            return False

        # Worker 已完成，session.messages 已注入 hook 输出（worker 线程安全写入）

        # 通知 UI 刷新（主线程 emit）
        if self._backend and hook_mgr:
            self._backend._hook_messages_updated.emit()

        messages = worker._messages
        available_tools = worker._available_tools

        # ---- 使用 ConversationExecutor 执行（主线程，创建 QThread worker） ----
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
                "pruned_tokens": 0,
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
                    system_prompt = (
                        am.get_agent_system_prompt(
                            self._current_agent,
                            is_subagent_call=False,
                            extra_context=extra_context,
                        )
                        or ""
                    )
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
        # S2: 工具结果截断投影 — 与 context_builder.build_messages 使用同一
        # prune_tool_result（同参数），使 UI 估算 = 实际发送 token（截断后），
        # 保证「投影与实际用量一致」；并累计节省量 pruned_tokens 供 UI 展示。
        # 仅对超阈值消息建副本，不修改 session.messages 原始存储（可追溯）。
        pruned_tokens = 0
        for _m in session.messages:
            _content = _m.get("content")
            if _m.get("role") == "tool" and isinstance(_content, str) and len(_content) > TOOL_RESULT_MAX_LEN:
                raw_tok = per_message_tokens(_m, model)
                _m_copy = dict(_m)
                _m_copy["content"] = prune_tool_result(_content)
                approx_messages.append(_m_copy)
                pruned_tokens += max(0, raw_tok - per_message_tokens(_m_copy, model))
            else:
                approx_messages.append(_m)

        # 获取工具 schema（与实际 API 请求一致），必须计入上下文占用
        # ⚠️ 旧实现此处漏传 tools，导致工具定义（35+ 工具）的 token 完全未计入，
        # 是本地估算与 API 返回的 prompt_tokens 差异巨大的主因之一。
        #
        # 🚀 性能优化：30 秒缓存 tools 列表和 tools_tokens，避免每次工具执行后
        # 的上下文刷新都触发 get_agent_tools_schema → get_builtin_tools_schema
        # （后者做 deepcopy + MCP/LSP/subagent 动态注入，即使 5 秒 TTL 也常因
        # 工具执行间隔 >5 秒而失效浪费）。
        import time as _time

        _cache = self._tools_schema_cache
        _now = _time.monotonic()
        _CACHE_TTL = 30.0
        if _now - _cache["timestamp"] < _CACHE_TTL and _cache["tools"] is not None:
            available_tools = _cache["tools"]
            tools_tokens = _cache["tokens"]
        else:
            if self._current_agent:
                available_tools = self._get_agent_manager().get_agent_tools_schema(
                    self._current_agent,
                    builtin_tools=self._tool_executor._builtin_tools if self._tool_executor else None,
                )
            else:
                available_tools = get_builtin_tools_schema(
                    self._get_agent_manager(),
                    builtin_tools=self._tool_executor._builtin_tools if self._tool_executor else None,
                )
            # 更新缓存
            tools_tokens = (
                int(count_tools_tokens(available_tools, model) * get_model_token_ratio(model)) if available_tools else 0
            )
            _cache["timestamp"] = _now
            _cache["tools"] = available_tools
            _cache["tokens"] = tools_tokens

        budget_tokens = max(1, self._conversation_core.context_builder.get_context_budget(llm_config))
        # 估算总量：系统提示 + 全部消息 + 工具定义（含模型分词校正系数）
        # 性能优化（O-01）：用 per_message_tokens 累加替代 count_messages_tokens(messages)，
        # 避免内部 is 身份缓存的 [msg] 临时列表构造与查找开销。
        # est_total = sum(per_message_tokens) + tools_tokens，保证与下方 breakdown 之和
        # 内部自洽（scale 等比缩放基线准确）。
        est_total = sum(per_message_tokens(m, model) for m in approx_messages) + tools_tokens

        # ---- 各类型上下文占比（按角色拆分，用于 WorkBuddy 风格占比条）----
        system_tokens = per_message_tokens({"role": "system", "content": system_prompt}, model) if system_prompt else 0

        # 按消息角色拆分：用户消息 / 助手消息 / 工具结果 / Hook 注入
        # 每条消息独立计 token，且不含工具 schema 开销（工具定义单独计在 tools_tokens），
        # 避免与下面的 工具定义 重复计入。
        # 带 _hook_event 标记的消息是 hook 注入的动态上下文（如长期记忆、系统时间等），
        # 独立统计以便用户直观了解 hook 机制对上下文的占用。
        # 性能优化（O-01）：用 per_message_tokens 替代 count_messages_tokens([msg])，
        # 消除 [msg] 临时列表分配 + 4-entry is 缓存必然 MISS 的开销，
        # 100 条消息场景从 ~102 次 count_messages_tokens 降至 ~N+1 次 per_message_tokens。
        user_tokens = assistant_tokens = tool_tokens = hook_tokens = 0
        for msg in session.messages:
            role = msg.get("role", "")
            t = per_message_tokens(msg, model)
            # 分离 hook 注入消息（带 _hook_event 标记），独立统计
            if msg.get("_hook_event"):
                hook_tokens += t
            elif role == "user":
                user_tokens += t
            elif role == "assistant":
                assistant_tokens += t
            elif role == "tool":
                # S2: 工具结果按实际发送口径统计（超阈值先截断再计 token）
                _m = msg
                if isinstance(msg.get("content"), str) and len(msg["content"]) > TOOL_RESULT_MAX_LEN:
                    _m = dict(msg)
                    _m["content"] = prune_tool_result(_m["content"])
                tool_tokens += per_message_tokens(_m, model)
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
                # 性能优化（O-01）：per_message_tokens 累加，避免临时列表+缓存 MISS
                new_msgs = session.messages[api_message_count:]
                delta = sum(per_message_tokens(m, model) for m in new_msgs)
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
                # 性能优化（O-01）：per_message_tokens 直接处理单条 summary_msg
                compacted_tokens = per_message_tokens(summary_msg, model)
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
            "pruned_tokens": pruned_tokens,
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
        project_root = (
            self._backend.tool_executor.get_workdir() if self._backend and self._backend.tool_executor else None
        )
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
            trigger_async=False,  # 🛡️ F1(W1-C1)：显式同步，恢复改动前语义。
            # 异步路径（默认 trigger_async=True）会让 command hook 输出经
            # on_hook_finished → _hook_message_queue → chat_worker 注入 LLM 对话，
            # 而旧行为是同步执行、输出仅在返回值中（此处不消费）→ 静默丢弃。
            # 若走异步，每条 assistant 消息后会多出一条注入消息污染上下文。
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


# ============================================================
# _PreSendWorker — 后台线程执行 hooks + build_messages
# ============================================================


class _PreSendWorker(QThread):
    """在后台线程执行消息预处理的 Worker。

    负责：hook 触发 + session 注入 + build_messages + tool schema 获取。
    所有操作不涉及 GUI，可以在非主线程安全执行。

    QEventLoop + QThread 模式：主线程通过 loop.exec() 等待，
    期间可继续处理 UI 事件（绘制、鼠标、定时器）。
    """

    def __init__(
        self,
        *,
        hook_mgr,
        session,
        user_text: str,
        content_to_store,
        user_prompt_ctx: dict | None,
        pre_user_ctx: dict | None,
        post_user_ctx: dict | None,
        llm_config: dict,
        adapter,
        current_agent: str | None,
        window_workdir: str,
        agent_manager,
        tool_executor,
        hook_event: str | None = None,
    ):
        super().__init__()
        self._hook_mgr = hook_mgr
        self._session = session
        self._user_text = user_text
        self._content_to_store = content_to_store
        self._user_prompt_ctx = user_prompt_ctx
        self._pre_user_ctx = pre_user_ctx
        self._post_user_ctx = post_user_ctx
        self._llm_config = llm_config
        self._adapter = adapter
        self._current_agent = current_agent
        self._window_workdir = window_workdir
        self._agent_manager = agent_manager
        self._tool_executor = tool_executor
        self._hook_event = hook_event

        # 结果
        self._messages: list = []
        self._available_tools: list = []
        self._error: str | None = None

        # 线程安全锁（保护 _session.messages 写入）
        self._lock = threading.Lock()

    def run(self):
        """在后台线程执行所有预处理工作。"""
        try:
            self._do_hooks_and_build()
        except Exception as e:
            logger.exception(f"[PreSendWorker] Unexpected error: {e}")
            self._error = str(e)

    def _do_hooks_and_build(self):
        """执行 hooks → 注入 session → build_messages → tools"""
        from app.core.backend import _inject_hook_to_session
        from app.tools import get_builtin_tools_schema

        hook_mgr = self._hook_mgr
        session = self._session
        user_text = self._user_text
        window_workdir = self._window_workdir

        # ---- 辅助：触发 hook 并注入 session ----
        def _trigger_and_inject(event_name, extra_context, inject_to_session):
            if hook_mgr is None or inject_to_session is None:
                return
            ctx = {
                "project_root": window_workdir,
                "current_role": "primary",
                "is_subagent_call": False,
            }
            if extra_context:
                ctx.update(extra_context)
            results = hook_mgr.trigger_event(
                event_name,
                context=ctx,
                current_message=user_text,
                trigger_async=False,
            )
            with self._lock:
                for r in results:
                    if r.success and r.output:
                        _inject_hook_to_session(inject_to_session, event_name, r.output, r.status_message)

        # ---- 1. UserPromptSubmit hooks ----
        _trigger_and_inject("UserPromptSubmit", self._user_prompt_ctx, session)

        # ---- 2. PreUserMessage hooks ----
        _trigger_and_inject("PreUserMessage", self._pre_user_ctx, session)

        # ---- 3. 添加用户消息 ----
        with self._lock:
            _add_kwargs = {}
            if self._hook_event:
                _add_kwargs["_hook_event"] = self._hook_event
            session.add_user_message(content=self._content_to_store, **_add_kwargs)

        # ---- 4. PostUserMessage hooks ----
        _trigger_and_inject("PostUserMessage", self._post_user_ctx, session)

        # ---- 5. build_messages ----
        self._messages = self._adapter.build_messages(
            self._session,  # 使用捕获的 session，避免 loop.exec() 期间会话切换导致不一致
            self._llm_config,
            current_agent=self._current_agent,
        )

        # ---- 6. 获取 tool schema ----
        if self._current_agent:
            self._available_tools = self._agent_manager.get_agent_tools_schema(
                self._current_agent,
                builtin_tools=self._tool_executor._builtin_tools if self._tool_executor else None,
            )
        else:
            self._available_tools = get_builtin_tools_schema(
                self._agent_manager,
                builtin_tools=self._tool_executor._builtin_tools if self._tool_executor else None,
            )
