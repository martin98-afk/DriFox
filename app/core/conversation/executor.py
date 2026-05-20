# app/core/conversation/executor.py
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from app.core.agent import PermissionResolver
from app.core.conversation.config import ConversationConfig, PermissionStrategy
from app.core.conversation.core import ConversationCore
from app.core.workers.chat_worker import OpenAIChatWorker


class ConversationExecutor:
    """统一对话执行器

    职责：
    1. 根据 ConversationConfig 的权限策略生成权限检查闭包
    2. 创建 OpenAIChatWorker 并连接回调
    3. 管理 Worker 生命周期（启动/停止/清理）
    """

    def __init__(
        self,
        core: ConversationCore,
        config: ConversationConfig,
        tool_executor: Any,
        agent_manager: Any,
    ):
        self._core = core
        self._config = config
        self._tool_executor = tool_executor
        self._agent_manager = agent_manager

        self._current_worker: Optional[OpenAIChatWorker] = None
        self._is_streaming = False

    @property
    def is_streaming(self) -> bool:
        return self._is_streaming

    def get_current_worker(self):
        """获取当前 Worker 实例（供外部直接连接信号）"""
        return self._current_worker

    def execute(
        self,
        messages: List[Dict],
        llm_config: Dict,
        tools: List[Dict],
        callbacks: Optional[Dict[str, Callable]] = None,
    ) -> bool:
        """创建 Worker 并启动

        Args:
            messages: 发送给 LLM 的消息列表
            llm_config: LLM 配置
            tools: 工具 schema 列表
            callbacks: 回调字典 {
                "content_received": fn(chunk),
                "reasoning_content_received": fn(piece),
                "thinking_started": fn(),
                "tool_call_started": fn(id, name, args, round),
                "tool_args_updated": fn(id, name, partial),
                "tool_result_received": fn(id, name, args, result),
                "question_asked": fn(id, question, options, multiple),
                "permission_approval_requested": fn(id, name, args),
                "finished": fn(response),
                "messages_updated": fn(messages),
                "error": fn(error),
                "retry_status": fn(error_type, attempt, max_retries, wait_time),
                "stream_started": fn(),
            }

        Returns:
            True 如果 Worker 已启动
        """
        if self._is_streaming:
            logger.warning("[ConversationExecutor] Already streaming")
            return False

        callbacks = callbacks or {}
        session = self._core.session_manager.get_current_session()

        # 获取 compaction 提示词
        compaction_prompt = ""
        compaction_config = {}
        if self._agent_manager and self._agent_manager.get_agent("compaction"):
            compaction_prompt = self._agent_manager.get_agent_system_prompt("compaction")
            compaction_config = self._agent_manager.get_agent_config("compaction")

        # 清理旧 Worker
        self.cleanup()

        # 创建 Worker
        self._current_worker = OpenAIChatWorker(
            messages=messages,
            session_messages=session.get_context_messages() if session else [],
            llm_config=llm_config,
            tools=tools,
            tool_executor=self._tool_executor,
            tool_start_callback=callbacks.get("tool_call_sync_requested"),
            permission_check_callback=self._make_permission_checker(),
            compaction_prompt=compaction_prompt,
            compaction_config=compaction_config,
            permission_cache=self._core.permission_cache,
            compactor=self._core.compactor,
            initial_compaction_cache=getattr(session, "compaction_cache", None),
        )

        # 连接回调
        self._connect_callbacks(callbacks)

        # Worker 完成后重置流式状态（start 前连接，避免竞态）
        self._current_worker.finished.connect(self._on_worker_finished)

        # 启动
        self._is_streaming = True
        self._current_worker.start()

        # 通知 stream 开始
        cb = callbacks.get("stream_started")
        if cb:
            cb()

        return True

    def _on_worker_finished(self):
        """Worker 线程结束，重置流式状态"""
        self._is_streaming = False
        self._current_worker = None

    def _make_permission_checker(self) -> Optional[Callable[[str, dict], str]]:
        """根据权限策略生成权限检查器"""
        strategy = self._config.permission_strategy

        if strategy == PermissionStrategy.INTERACTIVE:
            cb = self._config.interactive_check_callback
            if cb:
                return cb
            logger.warning("[ConversationExecutor] INTERACTIVE strategy without callback, defaulting to allow")
            return lambda tool_name, args: "allow"

        if strategy == PermissionStrategy.AUTO_ALLOW:
            return lambda tool_name, args: "allow"

        if strategy == PermissionStrategy.AUTO_DENY:
            return lambda tool_name, args: "deny"

        if strategy == PermissionStrategy.AGENT_CONFIG:
            return self._make_agent_config_checker()

        logger.warning(f"[ConversationExecutor] Unknown strategy: {strategy}, defaulting to allow")
        return lambda tool_name, args: "allow"

    def _make_agent_config_checker(self) -> Callable[[str, dict], str]:
        """按 Agent 配置检查，ask 视为 deny"""
        resolver = PermissionResolver(
            self._config.agent_permission_config, {}, {}
        )

        def check(tool_name: str, arguments: dict) -> str:
            result = resolver.resolve(tool_name)
            if result == "ask":
                return "deny"
            return result

        return check

    def _connect_callbacks(self, callbacks: Dict[str, Callable]):
        """连接 Worker 信号到回调"""
        if not self._current_worker:
            return

        worker = self._current_worker

        def safe_connect(signal_name: str, callback_key: str):
            cb = callbacks.get(callback_key)
            if not cb:
                return
            signal = getattr(worker, signal_name, None)
            if signal is not None:
                signal.connect(cb)

        safe_connect("content_received", "content_received")
        safe_connect("reasoning_content_received", "reasoning_content_received")
        safe_connect("thinking_started", "thinking_started")
        safe_connect("tool_call_started", "tool_call_started")
        safe_connect("tool_args_updated", "tool_args_updated")
        safe_connect("tool_result_received", "tool_result_received")
        safe_connect("error_occurred", "error")
        safe_connect("finished_with_content", "finished")
        safe_connect("finished_with_messages", "messages_updated")
        safe_connect("question_asked", "question_asked")
        safe_connect("permission_approval_requested", "permission_approval_requested")
        safe_connect("retry_status", "retry_status")

    def stop(self) -> List[Dict]:
        """停止当前 Worker，返回中断的消息"""
        self._is_streaming = False
        worker = self._current_worker
        self._current_worker = None

        interrupted: List[Dict] = []
        if worker:
            try:
                interrupted = worker.get_interrupted_messages()
            except Exception as e:
                logger.warning(f"[ConversationExecutor] Failed to get interrupted messages: {e}")
            worker.cancel()
            if worker.isRunning():
                worker.quit()
            try:
                worker.cleanup()
            except Exception as e:
                logger.warning(f"[ConversationExecutor] Failed to cleanup worker: {e}")

        return interrupted

    def cleanup(self):
        """清理当前 Worker"""
        if self._current_worker:
            try:
                self._current_worker.cancel()
                if self._current_worker.isRunning():
                    self._current_worker.quit()
                self._current_worker.cleanup()
            except Exception as e:
                logger.warning(f"[ConversationExecutor] Cleanup error: {e}")
            self._current_worker = None
        self._is_streaming = False
