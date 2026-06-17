# -*- coding: utf-8 -*-
"""
UI 对话引擎 — 处理桌面 LLM 对话的核心逻辑

从 app/core/chat_engine.py 迁移而来，类名改为 UIEngine。
保留 ChatEngine 别名在 __init__.py 中提供向后兼容。
"""
import os
from typing import Dict, List, Optional, Callable, Any

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
from app.core.token_estimator import count_messages_tokens
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
        self._current_agent: Optional[str] = "plan"

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
        self._adapter.permission_approval_requested.connect(lambda i, n, a: self._emit("permission_approval_requested", i, n, a))
        self._adapter.stream_finished.connect(lambda r: self._on_worker_finished(r))
        self._adapter.messages_updated.connect(lambda ms: self._emit("messages_updated", ms))
        self._adapter.error_occurred.connect(lambda e: self._on_error(e))
        self._adapter.retry_status.connect(lambda *a: self._emit("retry_status", *a))
        self._adapter.retry_resolved.connect(lambda: self._emit("retry_resolved"))
        self._adapter.stream_started.connect(lambda: self._emit("stream_started"))

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
        return getattr(self._conversation_executor, '_current_worker', None)

    # ========== 属性访问（正式接口）==========

    @property
    def current_agent(self) -> str:
        """获取当前 Agent"""
        return self._current_agent or "plan"

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
        if hasattr(self, '_conversation_core'):
            self._conversation_core.session_manager = session_manager

    # ========== 权限检查 ==========

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

    def _on_permission_approval_requested(
        self, tool_call_id: str, tool_name: str, arguments: dict
    ):
        self._emit("permission_approval_requested", tool_call_id, tool_name, arguments)

    def approve_tool_permission(self, tool_call_id: str, auto_allow: bool = False, session_allow: bool = False):
        """批准工具权限请求（转发给 Worker）"""
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

    # ========== 消息发送 ==========

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

        # 公共辅助方法：触发 hook
        def _do_trigger(hook_mgr, event_name, extra_context=None, msg_text=None):
            if extra_context is None:
                extra_context = {}
            ctx = {"project_root": os.getcwd()}
            ctx.update(extra_context)
            hook_mgr.trigger_event(
                event_name,
                context=ctx,
                current_message=msg_text or user_text,
            )

        hook_mgr = getattr(self._agent_manager, '_hook_manager', None) if self._agent_manager else None

        if hook_mgr:
            _do_trigger(hook_mgr, "PreUserMessage", {"message": user_text})
        session.add_user_message(content=user_text)
        if hook_mgr:
            _do_trigger(hook_mgr, "PostUserMessage", {"message": user_text})

        # PreAssistantMessage
        if hook_mgr:
            last_user_msg = ""
            session_for_hook = self._session_manager.get_current_session()
            if session_for_hook and hasattr(session_for_hook, 'messages'):
                for msg in reversed(session_for_hook.messages):
                    if msg.get('role') == 'user':
                        last_user_msg = msg.get('content', '')
                        break
            _do_trigger(hook_mgr, "PreAssistantMessage", msg_text=last_user_msg)

        messages = self._adapter.build_messages(
            self._session_manager.get_current_session(),
            llm_config,
            current_agent=self._current_agent,
        )
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
            compaction_cache = getattr(session, "compaction_cache", {}) or {}
            summary_msg = compaction_cache.get("summary_message")
            if summary_msg:
                compacted_tokens = count_messages_tokens([summary_msg])
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

        # Trigger PostAssistantMessage hook（成功路径）
        self._trigger_post_assistant_message(response, is_error=False)

        # 保存缓存统计（在 worker 被清理前）
        self._save_cache_stats()
        # 对话结束后清理 worker，释放内存
        self.cleanup()

    def _save_cache_stats(self):
        """保存 Worker 的缓存统计到 Backend（Worker 被清理后仍可访问）"""
        from loguru import logger
        worker = getattr(self._conversation_executor, '_current_worker', None)
        if not worker:
            logger.debug("[_save_cache_stats] No worker found")
            return
        try:
            if hasattr(worker, 'get_cache_stats'):
                stats = worker.get_cache_stats()
                if stats:
                    # stats 可能是 dict 或带 to_dict() 的对象
                    if isinstance(stats, dict):
                        stats_dict = stats
                    else:
                        stats_dict = stats.to_dict()
                    hit_rate = stats_dict.get('hit_rate', 0.0)
                    cache_read = stats_dict.get('cache_read_tokens', 0)
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
        hook_mgr = getattr(self._agent_manager, '_hook_manager', None) if self._agent_manager else None
        if not hook_mgr:
            return

        session = self._session_manager.get_current_session()
        last_user_msg = ""
        if session and hasattr(session, 'messages'):
            for msg in reversed(session.messages):
                if msg.get('role') == 'user':
                    last_user_msg = msg.get('content', '')
                    break

        context = {
            "project_root": os.getcwd(),
        }
        if is_error:
            context["error"] = response_or_error
        else:
            context["response"] = response_or_error

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
        worker = getattr(self._conversation_executor, '_current_worker', None)
        if worker and hasattr(worker, "provide_answer"):
            worker.provide_answer(answer)

    def cleanup_worker(self):
        """清理当前 Worker 资源（用于切换会话前清理）

        由 Backend.cleanup_worker() 调用，委托 ConversationExecutor.cleanup() 实现。
        """
        self.cleanup()

    def get_current_worker(self):
        """获取当前 Worker 实例（供外部获取缓存统计等）"""
        return getattr(self._conversation_executor, '_current_worker', None)
