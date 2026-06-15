# app/core/conversation/executor.py
import threading
from typing import Any, Callable, Dict, List, Optional, Type

from loguru import logger

from app.core.agent import PermissionResolver
from app.core.conversation.config import ConversationConfig, PermissionStrategy
from app.core.conversation.core import ConversationCore
from app.core.workers.chat_worker import OpenAIChatWorker


class ConversationExecutor:
    """统一对话执行器

    职责：
    1. 根据 ConversationConfig 的权限策略生成权限检查闭包
    2. 根据可注入的 worker_factory 创建 Worker 并连接回调
    3. 管理 Worker 生命周期（启动/停止/清理）
    """

    def __init__(
        self,
        core: ConversationCore,
        config: ConversationConfig,
        tool_executor: Any,
        agent_manager: Any,
        worker_factory: Optional[Callable[..., Any]] = None,
    ):
        self._core = core
        self._config = config
        self._tool_executor = tool_executor
        self._agent_manager = agent_manager
        self._worker_factory = worker_factory or OpenAIChatWorker  # 默认用 OpenAIChatWorker

        self._current_worker: Optional[Any] = None  # 不再硬编码 OpenAIChatWorker
        self._is_streaming = False
        self._finalize_worker: Optional[Any] = None  # 两阶段停止：保存 worker 供 finalize_stop() 使用
        self._stop_lock = threading.Lock()  # 🛡️ 防止 cancel_worker/finalize_stop 多线程并发

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
            logger.warning(f"[ConversationExecutor] Already streaming (current_worker={type(self._current_worker).__name__ if self._current_worker else None}, is_alive={self._current_worker.isRunning() if self._current_worker else False})")
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

        # 创建 Worker（通过 factory 注入，支持替换为 MockWorker 等）
        worker_kwargs = {
            "messages": messages,
            "session_messages": session.get_context_messages() if session else [],
            "llm_config": llm_config,
            "tools": tools,
            "tool_executor": self._tool_executor,
            "tool_start_callback": callbacks.get("tool_call_sync_requested"),
            "permission_check_callback": self._make_permission_checker(),
            "compaction_prompt": compaction_prompt,
            "compaction_config": compaction_config,
            "permission_cache": self._core.permission_cache,
            "compactor": self._core.compactor,
            "initial_compaction_cache": getattr(session, "compaction_cache", None),
        }
        self._current_worker = self._worker_factory(**worker_kwargs)

        # 连接回调
        self._connect_callbacks(callbacks)

        # Worker 完成后重置流式状态（start 前连接，避免竞态）
        # 传入 worker 引用用于身份检查，防止旧 worker 的 finished 信号擦除新 worker
        current_worker = self._current_worker
        current_worker.finished.connect(
            lambda w=current_worker: self._on_worker_finished(w)
        )

        # 启动
        self._is_streaming = True
        self._current_worker.start()

        # 通知 stream 开始
        cb = callbacks.get("stream_started")
        if cb:
            cb()

        return True

    def _on_worker_finished(self, worker):
        """Worker 线程结束，重置流式状态

        Args:
            worker: 触发回调的 worker 实例。用于身份检查，
                    防止旧 worker 的 finished 信号擦除新 worker（竞态条件 RC1）。
        """
        logger.info(f"[ConversationExecutor] _on_worker_finished called: worker={type(worker).__name__}, current_worker={type(self._current_worker).__name__ if self._current_worker else None}, is_match={worker is self._current_worker}")
        if worker is not self._current_worker:
            # 旧 worker 的 finished 信号，忽略（新 worker 已创建）
            return
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

    def cancel_worker(self):
        """非阻塞取消当前 Worker

        仅执行非阻塞操作：
        1. 标记流式已停止
        2. 断开所有信号连接
        3. 调用 worker.cancel() 设置取消标志
        4. 保存 worker 引用供 finalize_stop() 使用

        不等待 worker 线程结束，不获取中断消息，不清理资源。
        调用后需在适当时机调用 finalize_stop() 完成后续操作。
        """
        with self._stop_lock:
            self._is_streaming = False
            worker = self._current_worker
            self._current_worker = None

            if worker:
                # 断开所有信号连接，防止已取消的 worker 继续向 UI 发送事件
                # ⚠️ 重要：不断开 `finished` 信号（QThread.finished），
                # AutoLoopWorker 的主循环 QEventLoop 依赖此信号退出 wait 状态。
                # 断开它会导致 QEventLoop 永久挂起，进而触发闪退。
                for signal_name in ("retry_status", "error_occurred", "finished_with_content",
                                    "finished_with_messages", "content_received", "reasoning_content_received",
                                     "tool_call_started", "tool_args_updated", "tool_result_received",
                                     "question_asked", "permission_approval_requested", "thinking_started",
                                     "retry_resolved", "compaction_status_changed"):
                    try:
                        signal = getattr(worker, signal_name, None)
                        if signal is not None:
                            signal.disconnect()
                    except (TypeError, RuntimeError):
                        pass

                # 设置取消标志（非阻塞，worker 线程会在下次检查时主动退出）
                worker.cancel()
                # 保存 worker 引用供 finalize_stop() 使用
                self._finalize_worker = worker

    def finalize_stop(self) -> List[Dict]:
        """完成停止流程（阻塞操作）

        在 cancel_worker() 调用后执行，等待 worker 线程结束并收集结果。
        此方法是阻塞的，应在 UI 更新后（或在后台线程中）调用。

        线程安全：_stop_lock 防止多线程（daemon 线程 + closeEvent）同时
        调用 finalize_stop 导致 worker 双重重释放。

        Returns:
            被中断的消息列表
        """
        # 🛡️ 加锁保护，防止与 daemon 线程的 finalize_stop 并发
        with self._stop_lock:
            worker = getattr(self, '_finalize_worker', None)
            self._finalize_worker = None

        interrupted: List[Dict] = []
        if not worker:
            return interrupted

        # 等待 worker 线程结束（最多等 3 秒）
        if worker.isRunning():
            if not worker.wait(3000):
                logger.warning(f"[ConversationExecutor] Worker did not finish within 3s, requesting interruption")
                worker.quit()
                worker.requestInterruption()
                if not worker.wait(1000):
                    # 🛡️ 安全停止：不调用 QThread.terminate()
                    # terminate() 会强制杀死 OS 线程，如果 Worker 恰好持有 GIL 或分配内存，
                    # 会导致 Python 解释器状态损坏 → 段错误闪退。
                    # 改用 requestInterruption() 请求线程退出（线程内检查 isInterruptionRequested()），
                    # 即使线程未及时退出，仅产生资源泄漏，远优于进程崩溃。
                    logger.warning(f"[ConversationExecutor] Worker still running after interruption request, proceeding with cleanup")

        # worker 已停止，状态已稳定，安全获取中断消息
        try:
            interrupted = worker.get_interrupted_messages()
        except Exception as e:
            logger.warning(f"[ConversationExecutor] Failed to get interrupted messages: {e}")

        # 清理 worker 资源
        try:
            worker.cleanup()
        except Exception as e:
            logger.warning(f"[ConversationExecutor] Failed to cleanup worker: {e}")

        return interrupted

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
        safe_connect("retry_resolved", "retry_resolved")

    def stop(self) -> List[Dict]:
        """停止当前 Worker，返回中断的消息（同步方式，可能阻塞 UI 线程）

        内部使用两阶段停止：
        1. cancel_worker() - 非阻塞操作
        2. finalize_stop() - 阻塞操作（wait + 获取消息 + 清理）

        如果需要在 UI 线程中快速响应，建议分别调用：
          executor.cancel_worker()        # 立即返回
          QTimer.singleShot(0, lambda: ... executor.finalize_stop())
        """
        self.cancel_worker()
        return self.finalize_stop()

    def cleanup(self):
        """清理当前 Worker"""
        if self._current_worker:
            worker = self._current_worker
            try:
                # 断开信号，防止旧 worker 的 finished 信号影响新 worker
                for signal_name in ("finished",):
                    try:
                        signal = getattr(worker, signal_name, None)
                        if signal is not None:
                            signal.disconnect()
                    except (TypeError, RuntimeError):
                        pass
                worker.cancel()
                worker.requestInterruption()
                if worker.isRunning():
                    worker.quit()
                    # 🛡️ 等待 Worker 实际退出后再清理，防止 cleanup() 与 Worker
                    # 线程同时访问 _event_bus、_http_client、messages 等属性
                    # 导致线程不安全的数据竞争。
                    worker.wait(2000)
                worker.cleanup()
            except Exception as e:
                logger.warning(f"[ConversationExecutor] Cleanup error: {e}")
            self._current_worker = None
        self._is_streaming = False
