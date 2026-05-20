# -*- coding: utf-8 -*-
"""
聊天引擎模块 - 处理 LLM 对话的核心逻辑
"""
from typing import Dict, List, Optional, Callable, Any

from loguru import logger

from app.core.agent import PermissionResolver
from app.core.chat_session import (
    ChatSession,
    SessionManager,
)
from app.core.conversation.core import ConversationCore
from app.core.conversation.config import ConversationConfig, PermissionStrategy
from app.core.conversation.adapters import UIConversationAdapter
from app.core.conversation.adapters import UIConversationAdapter
from app.core.message_content import (
    consolidate_messages,
)
from app.core.token_estimator import count_messages_tokens
from app.tools import get_builtin_tools_schema


class ChatEngine:
    """聊天引擎，负责组装上下文并驱动 worker。"""

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
        self._current_agent: Optional[str] = "plan"

        # API 模式专用：直接回调（绕过 Qt 信号-槽，避免跨线程事件循环问题）
        self._worker_callbacks = worker_callbacks or {}
        self._api_mode = api_mode

        # ===== ConversationCore（聚合 Compactor + PermissionCache + ContextBudgetAllocator）=====
        self._conversation_core = ConversationCore.create(
            get_model_config=get_model_config,
            agent_manager=agent_manager,
            backend=backend,
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

        # ===== Adapter 信号 → ChatEngine 回调 =====
        self._adapter.content_received.connect(lambda p: self._emit("content_received", p))
        self._adapter.reasoning_content_received.connect(lambda p: self._emit("reasoning_content_received", p))
        self._adapter.thinking_started.connect(lambda: self._emit("thinking_started"))
        self._adapter.tool_call_started.connect(lambda i, n, a, r: self._emit("tool_call_started", i, n, a, r))
        self._adapter.tool_args_updated.connect(lambda i, n, p: self._emit("tool_args_updated", i, n, p))
        self._adapter.tool_result_received.connect(lambda i, n, a, r: self._emit("tool_result_received", i, n, a, r))
        self._adapter.question_asked.connect(lambda i, q, o, m: self._emit("question_asked", i, q, o, m))
        self._adapter.permission_approval_requested.connect(lambda i, n, a: self._emit("permission_approval_requested", i, n, a))
        self._adapter.stream_finished.connect(lambda r: self._on_worker_finished(r))
        self._adapter.messages_updated.connect(lambda ms: self._emit("messages_updated", ms))
        self._adapter.error_occurred.connect(lambda e: self._on_error(e))
        self._adapter.retry_status.connect(lambda *a: self._emit("retry_status", *a))
        self._adapter.stream_started.connect(lambda: self._emit("stream_started"))

    @property
    def compactor(self):
        """暴露压缩器供外部使用（如工具迭代中压缩）"""
        return self._conversation_core.compactor

    # 向后兼容属性（外部代码通过 chat_engine._permission_cache / _compactor / _current_worker 访问）
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
        return getattr(self._conversation_executor, '_current_worker', None)
    
    # ========== 属性访问（正式接口，避免直接访问私有属性）==========
    
    @property
    def current_agent(self) -> str:
        """获取当前 Agent"""
        return self._current_agent or "plan"
    
    def set_current_agent(self, value: str):
        """设置当前 Agent"""
        self._current_agent = value
    
    @property
    def is_streaming(self) -> bool:
        """获取流式状态（委托 ConversationExecutor）"""
        return self._conversation_executor.is_streaming
    
    def set_streaming(self, value: bool):
        """设置流式状态"""
        # 已由 ConversationExecutor 管理，忽略手动设置
    
    def get_context_usage(self) -> tuple:
        """获取上下文使用情况（用于兼容性）"""
        return (0, 0)  # 占位，后续可实现

    def _get_agent_manager(self):
        return self._agent_manager

    def set_session_manager(self, session_manager):
        """Update the session manager reference (used when session is archived)."""
        self._session_manager = session_manager

    def _check_tool_permission(self, tool_name: str, arguments: dict) -> str:
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
            elif tool_name in ("read", "edit", "write", "patch", "multiedit"):
                file_path = arguments.get("filePath", "")
                return perm_resolver.resolve(tool_name, file_path)
            elif tool_name == "webfetch":
                url = arguments.get("url", "")
                return perm_resolver.resolve(tool_name, url)
            elif tool_name == "websearch":
                query = arguments.get("query", "")
                return perm_resolver.resolve(tool_name, query)
            elif tool_name == "task_batch":
                # task_batch 支持多个子智能体，检查第一个的权限
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

    def _on_permission_approval_requested(
        self, tool_call_id: str, tool_name: str, arguments: dict
    ):
        self._emit("permission_approval_requested", tool_call_id, tool_name, arguments)

    def approve_tool_permission(self, tool_call_id: str, auto_allow: bool = False, session_allow: bool = False):
        """批准工具权限请求（转发给 Worker）"""
        from app.core.workers import OpenAIChatWorker as W
        # 通过 executor 获取当前 worker（可通过 protected 属性访问）
        worker = getattr(self._conversation_executor, '_current_worker', None)
        if worker:
            worker.approve_permission(tool_call_id, auto_allow, session_allow)

    def deny_tool_permission(self, tool_call_id: str):
        worker = getattr(self._conversation_executor, '_current_worker', None)
        if worker:
            worker.deny_permission(tool_call_id)

    def clear_session_permission_cache(self, tool_name: str = None):
        """清除会话级权限缓存"""
        cache = self._conversation_core.permission_cache
        if tool_name:
            cache.deny(tool_name)
        else:
            cache.clear_session()

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

    @property
    def session_manager(self) -> SessionManager:
        return self._session_manager

    def switch_agent(self, agent_name: Optional[str]):
        agent_manager = self._get_agent_manager()
        if agent_name is None or agent_name.lower() in ("default", "通用"):
            self._current_agent = "plan"
            logger.info("[ChatEngine] Switched to default agent: plan")
            self._emit("agent_switched", "plan")
            return

        agent = agent_manager.get_agent(agent_name)
        if not agent:
            logger.warning(f"[ChatEngine] Agent not found: {agent_name}")
            return

        self._current_agent = agent_name
        logger.info(f"[ChatEngine] Switched to agent: {agent_name}")
        self._emit("agent_switched", agent_name)

    def send_message(
        self,
        user_text: str,
        *args,
        **kwargs
    ) -> bool:
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

        # Trigger PreUserMessage hook
        if hasattr(self._agent_manager, '_hook_manager') and self._agent_manager._hook_manager:
            hook_manager = self._agent_manager._hook_manager
            if hook_manager:
                import os
                context = {
                    "project_root": os.getcwd(),
                    "message": user_text,
                }
                hook_manager.trigger_event(
                    "PreUserMessage",
                    context=context,
                    current_message=user_text
                )
        session.add_user_message(content=user_text)

        self._emit("user_message_added", user_text)

        # Trigger PostUserMessage hook
        if hasattr(self._agent_manager, '_hook_manager') and self._agent_manager._hook_manager:
            hook_manager = self._agent_manager._hook_manager
            if hook_manager:
                import os
                context = {
                    "project_root": os.getcwd(),
                    "message": user_text,
                }
                hook_manager.trigger_event(
                    "PostUserMessage",
                    context=context,
                    current_message=user_text
                )

        # Trigger PreAssistantMessage hook - BEFORE build_messages, so output is included
        if hasattr(self._agent_manager, '_hook_manager') and self._agent_manager._hook_manager:
            hook_manager = self._agent_manager._hook_manager
            if hook_manager:
                import os
                session_for_hook = self._session_manager.get_current_session()
                current_message_text = ""
                if session_for_hook and hasattr(session_for_hook, 'messages'):
                    for msg in reversed(session_for_hook.messages):
                        if msg.get('role') == 'user':
                            current_message_text = msg.get('content', '')
                            break
                context = {
                    "project_root": os.getcwd(),
                }
                hook_manager.trigger_event(
                    "PreAssistantMessage",
                    context=context,
                    current_message=current_message_text
                )

        messages = self._adapter.build_messages(self._session_manager.get_current_session(), llm_config)
        if self._current_agent:
            available_tools = self._get_agent_manager().get_agent_tools_schema(
                self._current_agent
            )
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
        self, session: Optional[ChatSession] = None, llm_config: Optional[Dict] = None
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
            }

        # 直接构建消息
        messages = self._build_messages(session, llm_config)
        
        budget_tokens = max(1, self._conversation_core.context_builder.get_context_budget(llm_config))
        used_tokens = count_messages_tokens(messages)
        percent = max(0, min(100, int((used_tokens / budget_tokens) * 100)))
        
        # 计算普通上下文和压缩上下文的 token 分解
        compaction = dict(getattr(session, "compaction_state", {}) or {})
        normal_tokens = used_tokens
        compacted_tokens = 0
        
        if compaction.get("active"):
            # 有压缩时，从 compaction_cache 获取摘要消息的 token 数
            compaction_cache = getattr(session, "compaction_cache", {}) or {}
            summary_msg = compaction_cache.get("summary_message")
            if summary_msg:
                compacted_tokens = count_messages_tokens([summary_msg])
                normal_tokens = used_tokens - compacted_tokens
            else:
                # 如果没有缓存的摘要消息，基于比例估算
                summarized_count = compaction.get("summarized_count", 0)
                kept_count = compaction.get("kept_count", 0)
                total_count = summarized_count + kept_count
                if total_count > 0:
                    compacted_tokens = int(used_tokens * summarized_count / total_count * 0.3)  # 压缩后约为原来的30%
                    normal_tokens = used_tokens - compacted_tokens
        
        return {
            "used_tokens": used_tokens,
            "budget_tokens": budget_tokens,
            "percent": percent,
            "compaction": compaction,
            "normal_tokens": normal_tokens,
            "compacted_tokens": compacted_tokens,
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
        self._emit("stream_finished", response)
        
        # Trigger PostAssistantMessage hook
        if self._agent_manager and hasattr(self._agent_manager, '_hook_manager'):
            hook_manager = self._agent_manager._hook_manager
            if hook_manager:
                session = self._session_manager.get_current_session()
                current_message_text = ""
                if session and hasattr(session, 'messages'):
                    # Find last user message
                    for msg in reversed(session.messages):
                        if msg.get('role') == 'user':
                            current_message_text = msg.get('content', '')
                            break
                import os
                context = {
                    "project_root": os.getcwd(),
                    "response": response,
                }
                hook_manager.trigger_event(
                    "PostAssistantMessage",
                    context=context,
                    current_message=current_message_text
                )
        
        # 对话结束后清理 worker，释放内存
        self.cleanup_worker()

    def _on_error(self, error: str):
        self._emit("error", error)

    def _on_retry_status(self, error_type: str, attempt: int, max_retries: int, wait_time: float):
        """API 重试状态通知"""
        self._emit("retry_status", error_type, attempt, max_retries, wait_time)

    def stop(self) -> List[Dict]:
        """停止当前 Worker（委托 ConversationExecutor）"""
        interrupted = self._conversation_executor.stop()
        # 通知 UI
        self._emit("stream_finished", "")
        return interrupted
    
    def cleanup_worker(self):
        """清理当前 Worker（委托 ConversationExecutor）"""
        self._conversation_executor.cleanup()

    def provide_question_answer(self, answer: str):
        worker = getattr(self._conversation_executor, '_current_worker', None)
        if worker and hasattr(worker, "provide_answer"):
            worker.provide_answer(answer)
