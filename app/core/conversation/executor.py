# app/core/conversation/executor.py
import threading
import time
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
        # B6：清理锁——防止 cleanup()/finalize_stop()/_on_worker_finished() 并发清理同一 worker
        self._cleanup_lock = threading.Lock()
        # B6：超时未退出 worker 的延迟清理队列（线程退出后由 finished 信号触发清理）
        self._pending_cleanup_workers: list = []
        self._watchdog_timer: Optional[threading.Timer] = None  # M10: 延迟清理看门狗

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
        direct_signals: bool = False,
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

        # 清理旧 Worker
        self.cleanup()

        session_id = session.session_id if session else ""

        # 创建 Worker（通过 factory 注入，支持替换为 MockWorker 等）
        worker_kwargs = {
            "messages": messages,
            "session_messages": session.get_context_messages() if session else [],
            "llm_config": llm_config,
            "tools": tools,
            "tool_executor": self._tool_executor,
            "tool_start_callback": callbacks.get("tool_call_sync_requested"),
            "permission_check_callback": self._make_permission_checker(),
            "permission_cache": self._core.permission_cache,
            "compactor": self._core.compactor,
            "initial_compaction_cache": getattr(session, "compaction_cache", None),
            "session_id": session_id,
            # Hook 参与级别：引擎经 ConversationConfig 声明，消息级 hook 拦截 +
            # 工具级 per-call 传参都由 worker 消费（未声明时默认 ALL 保持兼容）
            "hook_policy": getattr(self._config, "hook_policy", None),
            # 可选：HookPolicy 插件 id（优先级高于 hook_policy 枚举）
            "hook_policy_id": getattr(self._config, "hook_policy_id", None),
            # 可选：LoopPolicy 插件 id（引擎级声明，不改全局激活槽）
            "loop_policy_id": getattr(self._config, "loop_policy_id", None),
        }
        self._current_worker = self._worker_factory(**worker_kwargs)

        # 连接回调
        self._connect_callbacks(callbacks, direct_signals=direct_signals)

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
        logger.debug(f"[ConversationExecutor] _on_worker_finished called: worker={type(worker).__name__}, current_worker={type(self._current_worker).__name__ if self._current_worker else None}, is_match={worker is self._current_worker}")
        if worker is not self._current_worker:
            # 旧 worker 的 finished 信号上线时已不在 _current_worker（新 worker 已创建或
            # cancel_worker 已摘除）。线程已结束，仍需走统一收尾释放 QThread 对象
            # （T6-A：旧 worker 若不 deleteLater 同样残留）。
            self._finalize_worker_cleanup(worker)
            return
        self._is_streaming = False
        self._current_worker = None
        # 自然结束路径：worker 线程已结束，执行统一收尾（cleanup + deleteLater）
        # T6-A：之前该路径从未释放 QThread C++ 对象（executor 全文 0 次 deleteLater），
        # 每次对话完成残留一个 worker 对象 → 长对话/多标签页内存不回落。
        self._finalize_worker_cleanup(worker)

    # ========== Worker 统一收尾（T6-A + B6） ==========

    def _finalize_worker_cleanup(self, worker):
        """Worker 统一收尾：cleanup + deleteLater（线程已退出才安全执行）

        T6-A: PyQt5 的 QThread 对象由 C++ 持有（"To be destroyed by: C/C++"），
              必须 deleteLater() 排队销毁，否则 Python wrapper 残留。
        B6: 所有清理路径（finalize_stop/cleanup/_on_worker_finished/延迟清理）
              统一走此入口；_cleanup_lock 防止并发重复清理；线程仍在运行时
              转入延迟清理队列，不阻塞调用方。

        Args:
            worker: 待清理的 worker 实例。
        """
        if worker is None:
            return
        # 线程必须已退出才安全（避免与 worker 线程访问 _event_bus/消息等数据竞争）
        if worker.isRunning():
            logger.debug(f"[ConversationExecutor] worker 仍在运行，转入延迟清理: {type(worker).__name__}")
            self._schedule_deferred_cleanup(worker)
            return

        with self._cleanup_lock:
            # 状态守卫：已清理过的 worker 跳过，防止重复 deleteLater
            if getattr(worker, "_executor_cleaned", False):
                return
            try:
                worker.cleanup()
            except Exception as e:
                logger.warning(f"[ConversationExecutor] Failed to cleanup worker: {e}")
            # T6-A: deleteLater 释放 QThread C++ 对象（线程已退出，安全排队）
            try:
                worker.deleteLater()
            except Exception as e:
                logger.debug(f"[ConversationExecutor] deleteLater worker: {e}")
            worker._executor_cleaned = True

    def _schedule_deferred_cleanup(self, worker):
        """B6: 将超时未退出的 worker 转入延迟清理（线程退出后由 finished 信号触发收尾）

        M10: 队列元素改为 (worker, 入队时间戳) 元组，供看门狗判断超时。
        """
        if worker is None:
            return
        for w, _ in self._pending_cleanup_workers:
            if w is worker:
                return
        self._pending_cleanup_workers.append((worker, time.monotonic()))
        try:
            worker.finished.connect(
                lambda w=worker: self._on_deferred_worker_finished(w)
            )
        except (TypeError, RuntimeError):
            pass
        # 竞态兜底：连接 finished 时线程恰好已退出（信号已发出不会再触发），
        # 立即收尾（调用方此时不持有 _cleanup_lock，无重入风险）
        if not worker.isRunning():
            self._on_deferred_worker_finished(worker)
        # M10: 启动看门狗，兜底回收 finished 信号迟迟不触发的卡死 worker
        self._start_cleanup_watchdog()

    def _on_deferred_worker_finished(self, worker):
        """B6: worker 线程结束后执行延迟收尾（cleanup + deleteLater）"""
        for i, (w, _) in enumerate(self._pending_cleanup_workers):
            if w is worker:
                del self._pending_cleanup_workers[i]
                break
        self._finalize_worker_cleanup(worker)

    # ========== 延迟清理看门狗（M10：兜底回收卡死 worker） ==========

    def _start_cleanup_watchdog(self):
        """M10: 启动/续期看门狗（队列非空时周期扫描，防无限泄漏）"""
        if self._watchdog_timer is not None and self._watchdog_timer.is_alive():
            return
        if not self._pending_cleanup_workers:
            self._watchdog_timer = None
            return
        self._watchdog_timer = threading.Timer(30.0, self._cleanup_watchdog_scan)
        self._watchdog_timer.daemon = True
        self._watchdog_timer.start()

    def _cleanup_watchdog_scan(self):
        """M10: 扫描延迟清理队列，对 >60s 未退出的 worker 强制中断+回收（最后兜底）"""
        now = time.monotonic()
        for w, ts in list(self._pending_cleanup_workers):
            if now - ts <= 60.0:
                continue
            try:
                self._pending_cleanup_workers.remove((w, ts))
            except ValueError:
                pass
            # 卡死兜底：finished 信号未触发（线程卡死），绕过 _finalize_worker_cleanup
            # 的"运行则重排"逻辑，直接中断+deleteLater 回收，避免无限泄漏。
            if not getattr(w, "_executor_cleaned", False):
                try:
                    w.requestInterruption()
                    w.quit()
                    w.deleteLater()
                    w._executor_cleaned = True
                except Exception as e:
                    logger.debug(f"[ConversationExecutor] watchdog cleanup error: {e}")
        self._start_cleanup_watchdog()

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
            # 团队工具：无条件放行（schema 层已按团队身份过滤，执行层不再拦截）
            from app.tools.registry import ToolRegistry

            if tool_name in ToolRegistry.get_instance().team_only_tools():
                return "allow"
            # 权限参数适配（与 UI 引擎同口径）：bash 用 command、read 用 filePath...
            try:
                mode, arg = ToolRegistry.get_instance().permission_resolve_args(tool_name, arguments or {})
            except Exception:
                mode, arg = "plain", ""
            if mode == "task":
                result = resolver.resolve_task(arg)
            elif arg:
                result = resolver.resolve(tool_name, arg)
            else:
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
                # 断开业务流信号，防止已取消的 worker 继续向 UI 发送事件
                # ⚠️ 重要：保留以下 3 个"终结类"信号不断开：
                #   - `finished`（QThread.finished）：AutoLoopWorker 的主循环
                #     QEventLoop 依赖此信号退出 wait 状态。断开会导致 QEventLoop
                #     永久挂起，进而触发闪退。
                #   - `finished_with_content` / `finished_with_messages`：
                #     AutoLoopConversationAdapter 通过回调订阅这两个信号来
                #     set threading.Event，断开后 adapter.wait_for_completion
                #     会永久阻塞，进而在 5 秒兜底清理时触发
                #     "QThread: Destroyed while thread is still running"。
                for signal_name in ("retry_status", "error_occurred",
                                    "content_received", "reasoning_content_received",
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
                logger.warning("[ConversationExecutor] Worker did not finish within 3s, requesting interruption")
                worker.quit()
                worker.requestInterruption()
                if not worker.wait(1000):
                    # 🛡️ 安全停止：不调用 QThread.terminate()
                    # terminate() 会强制杀死 OS 线程，如果 Worker 恰好持有 GIL 或分配内存，
                    # 会导致 Python 解释器状态损坏 → 段错误闪退。
                    # 改用 requestInterruption() 请求线程退出（线程内检查 isInterruptionRequested()），
                    # 即使线程未及时退出，仅产生资源泄漏，远优于进程崩溃。
                    logger.warning("[ConversationExecutor] Worker still running after interruption request, proceeding with cleanup")

        # worker 已停止，状态已稳定，安全获取中断消息
        try:
            interrupted = worker.get_interrupted_messages()
        except Exception as e:
            logger.warning(f"[ConversationExecutor] Failed to get interrupted messages: {e}")

        # B6/T6-A: 统一收尾（cleanup + deleteLater）。若 worker 经 wait(3000)+wait(1000)
        # 仍超时未退出，_finalize_worker_cleanup 自动转入延迟清理队列，不在此阻塞。
        self._finalize_worker_cleanup(worker)

        return interrupted

    def _connect_callbacks(self, callbacks: Dict[str, Callable], direct_signals: bool = False):
        """连接 Worker 信号到回调

        direct_signals=True：强制 DirectConnection（在 worker 发射线程同步执行回调）。
        供 EngineSession 等后台线程同步等待场景：worker 若在无 Qt 事件循环的
        daemon 线程创建，AutoConnection 会把 finished 排队到该线程——事件永远
        不被处理，turn() 等到超时也收不到完成信号（跨线程队列黑洞）。
        回调方（_SyncAdapter）仅 set Event/存字段，线程安全。
        """
        if not self._current_worker:
            return

        worker = self._current_worker

        def safe_connect(signal_name: str, callback_key: str):
            cb = callbacks.get(callback_key)
            if not cb:
                return
            signal = getattr(worker, signal_name, None)
            if signal is not None:
                if direct_signals:
                    from PyQt5.QtCore import Qt

                    signal.connect(cb, Qt.DirectConnection)
                else:
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
        safe_connect("context_updated", "context_updated")

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
        """清理当前 Worker（B6: 超时转后台延迟清理，不阻塞线程安全收尾）"""
        with self._stop_lock:
            worker = self._current_worker
            self._current_worker = None
            self._is_streaming = False

        if worker is None:
            return

        try:
            # ⚠️ 不断开 finished 信号（T6-A 实测结论 + AutoLoop 兼容）：
            # 1. 实测：disconnect(finished) 后 deleteLater 的 QThread Python wrapper 无法回收
            #    （PyQt 内部引用表残留空壳）；保留连接则完全回收。
            # 2. AutoLoopWorker 的 QEventLoop 依赖 worker.finished → loop.quit 退出等待，
            #    此处 disconnect 会误断该连接导致 wait 挂起（原代码隐患）。
            # 3. 旧 worker 的 finished 触发 _on_worker_finished 有身份检查
            #    （worker is not self._current_worker → 走统一收尾），不会干扰新 worker。
            worker.cancel()
            worker.requestInterruption()
            if worker.isRunning():
                worker.quit()
                # B6: 等待退出（2000→1500ms）。超时不再同步 cleanup（避免与仍在运行的
                # worker 线程数据竞争），转入延迟清理队列，线程退出后由 finished 信号收尾。
                if not worker.wait(1500):
                    logger.warning(
                        "[ConversationExecutor] Worker did not stop within 1.5s, "
                        "deferring cleanup until thread exits"
                    )
                    self._schedule_deferred_cleanup(worker)
                    return
            # 线程已退出：统一收尾（cleanup + deleteLater，T6-A）
            self._finalize_worker_cleanup(worker)
        except Exception as e:
            logger.warning(f"[ConversationExecutor] Cleanup error: {e}")
