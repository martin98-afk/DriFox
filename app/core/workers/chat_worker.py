# -*- coding: utf-8 -*-
"""
Chat Worker - OpenAI 对话执行器
"""

import gc
import os
import re
import sys
import time
import concurrent.futures
import threading

# 可选依赖：psutil 用于内存诊断（不强制）
try:
    import psutil as _psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False
    _psutil = None
from datetime import datetime
from typing import Dict, List, Callable, Optional, Any

import httpcore
import httpx
import orjson as json
from PyQt5.QtCore import QThread, pyqtSignal, QCoreApplication
from PyQt5.QtWidgets import QApplication
from loguru import logger
from openai import (
    OpenAI, BadRequestError, RateLimitError, APIError, APIConnectionError,
    InternalServerError,
)

from app.constants import PARAM_SCHEMA, QUOTA_EXCLUDE_KEYS
from app.core.conversation.config import PermissionCache
from app.core.message_content import consolidate_messages, append_text_block, messages_to_api, to_api_message
from app.core.provider_profile import get_provider_profile
from app.core.model_capabilities import get_model_capabilities
from app.core.tool_call_parser import smart_parse_arguments
from app.core.workers.cache_tracker import CacheHitRateTracker
from app.core.workers.chat_worker_state import ChatWorkerState
from app.core.workers.worker_event_bus import WorkerEventBus, WorkerEvent

# 预编译正则表达式
_VALID_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class OpenAIChatWorker(QThread):
    content_received = pyqtSignal(str)
    reasoning_content_received = pyqtSignal(str)  # DeepSeek thinking mode
    thinking_started = pyqtSignal()  # 新一轮思考开始（多轮工具迭代时每轮触发）
    error_occurred = pyqtSignal(str)
    finished_with_content = pyqtSignal(str)
    finished_with_messages = pyqtSignal(list)
    compaction_status_changed = pyqtSignal(dict)
    tool_call_started = pyqtSignal(str, str, dict, str)
    tool_args_updated = pyqtSignal(str, str, dict)  # 工具参数流式更新 (tool_call_id, tool_name, partial_args)
    tool_result_received = pyqtSignal(str, str, dict, object)
    question_asked = pyqtSignal(str, list, object)  # id, questions, extra
    permission_approval_requested = pyqtSignal(str, str, dict)
    retry_status = pyqtSignal(str, int, int, float)  # error_type, attempt, max_retries, wait_time
    retry_resolved = pyqtSignal()  # 重试成功，恢复正常状态
    _DEFERRED_PREVIEW_TOOLS = {"question", "task", "todowrite", "todoread"}

    def __init__(
            self,
            messages: List[Dict],
            session_messages: List[Dict],
            llm_config: Dict,
            tools: List[Dict] = None,
            stream: bool = True,
            tool_executor=None,
            tool_start_callback=None,
            get_stage_prompt=None,
            stage_changed_callback=None,
            permission_check_callback=None,
            compaction_prompt: str = "",
            compaction_config: Dict = None,
            permission_cache: PermissionCache = None,
            compactor=None,
            initial_compaction_cache: Dict = None,
    ):
        super().__init__()
        self.messages = messages
        self.session_messages = consolidate_messages(session_messages or [])
        self.llm_config = llm_config
        self.tools = tools or []
        self.stream = stream
        self.tool_executor = tool_executor
        self.tool_start_callback = tool_start_callback
        self.get_stage_prompt = get_stage_prompt
        self.stage_changed_callback = stage_changed_callback
        self.permission_check_callback = permission_check_callback
        self.compaction_prompt = compaction_prompt
        self.compaction_config = compaction_config or {}
        
        # ========== 使用 ChatWorkerState 统一管理所有可变状态 ==========
        self._state = ChatWorkerState.from_constructor_args(
            messages=messages,
            session_messages=session_messages or [],
            llm_config=llm_config,
            tools=tools,
            compaction_prompt=compaction_prompt,
            compaction_config=compaction_config,
            permission_cache=permission_cache or PermissionCache(),
            event_bus=WorkerEventBus(),
            tool_executor=tool_executor,
            compactor=compactor,
            initial_compaction_cache=initial_compaction_cache,
        )
        self._sync_state_from_state()  # 同步到旧属性名（向后兼容）
        # ============================================================

        # ========== 性能优化：HTTP 客户端和参数缓存 ==========
        self._cached_api_config: Optional[Dict[str, Any]] = None  # 缓存的 API 配置
        self._max_param_retry_count = 10  # 最多重试10次（每收到一个 chunk 重试一次）
        self._cache_tracker = CacheHitRateTracker()  # 缓存命中率追踪器
        # ========== 工具结果持久化 (懒加载, 按 session_id 隔离) ==========
        self._result_persister: Optional["ToolResultPersister"] = None
        self._last_persist_stats: Optional[Dict[str, Any]] = None
        # 同步设置模型名称，用于模型感知的成本计算
        model_name = str(self.llm_config.get("模型名称", "") or "")
        if model_name:
            self._cache_tracker.set_model(model_name)

        # ========== 性能优化：API 消息缓存 ==========
        # 向后兼容：保留 PyQt Signal，但通过 EventBus 统一发射
        # UI 层连接这些 signal，事件总线负责分发到所有订阅者

        # 直接回调模式（API 层使用，已迁移到事件总线）
        # 保留以兼容旧的直接回调接口
        self._legacy_direct_callbacks: Dict[str, Callable] = {}

        # ========== HTTP 流式响应引用（供 cancel() 关闭连接）==========
        self._current_response: Any = None

        # ========== 内存诊断 ==========
        self._mem_diag_logged = False     # 防止重复日志刷屏
        self._mem_diag_iter_count = 0     # 诊断计数器（工具迭代轮次）
        self._mem_last_rss = 0.0          # 上一步 RSS 基线（MB）
        self._mem_total_chunks_logged = 0 # 累计记录的流式 chunk 数
        # 环境变量控制：MEM_DIAG=1 启用内存诊断（默认关闭）
        self._mem_diag_enabled = os.environ.get('MEM_DIAG') == '1'
        # tracemalloc 深度追踪（MEM_TRACE=1 时启用，用于定位单步大分配）
        self._mem_trace_enabled = os.environ.get('MEM_TRACE') == '1'
        self._mem_trace_snapshot = None

    def _get_persister(self) -> Optional["ToolResultPersister"]:
        """
        懒加载工具结果持久化器 (按 session_id 隔离)

        Returns:
            ToolResultPersister 实例, 或 None (初始化失败时)
        """
        if self._result_persister is None:
            try:
                from app.core.tool_result_persister import ToolResultPersister
                # 优先用 worker 当前 session_id, 兜底用 "default"
                session_id = (
                    getattr(self, "_current_session_id", None)
                    or getattr(self, "session_id", None)
                    or "default"
                )
                self._result_persister = ToolResultPersister(
                    session_id=str(session_id)
                )
            except Exception as e:
                logger.exception(f"[Persist] 初始化失败: {e}")
                return None
        return self._result_persister

    def _mem_take_trace(self):
        """在 before_api_call / after_api_call 之间采集 tracemalloc 快照"""
        if not self._mem_trace_enabled:
            return
        try:
            import tracemalloc
            if not tracemalloc.is_tracing():
                tracemalloc.start(25)
            gc.collect()
            snap = tracemalloc.take_snapshot()
            if self._mem_trace_snapshot is not None:
                # 对比上一次，输出热点
                diff = snap.compare_to(self._mem_trace_snapshot, 'lineno')
                large = [d for d in diff if d.size_diff > 10 * 1024 * 1024]
                if large:
                    large.sort(key=lambda d: d.size_diff, reverse=True)
                    log_path = _get_log_dir_path("mem_trace.log")
                    with open(log_path, 'a', encoding='utf-8') as f:
                        f.write(f"\n{'='*70}\n[MEM-TRACE] 分配 >10MB 的热点:\n{'='*70}\n")
                        f.write(f"{'增量MB':>8} {'总计MB':>8} {'计数':>5}  位置\n")
                        f.write(f"{'-'*70}\n")
                        for stat in large[:15]:
                            frame = stat.traceback[0]
                            loc = (frame.filename.split('site-packages')[-1]
                                   if 'site-packages' in frame.filename
                                   else frame.filename.split('python3')[-1]
                                   if 'python3' in frame.filename
                                   else frame.filename)
                            f.write(f"{stat.size_diff/1024/1024:>7.1f}M {stat.size/1024/1024:>7.1f}M {stat.count_diff:>4}  {loc}:{frame.lineno}\n")
                            for i, fr in enumerate(stat.traceback[:4]):
                                f.write(f"   {'├─' if i < 3 else '└─'} {fr.filename.split(os.sep)[-1]}:{fr.lineno} {fr.name}\n")
                    logger.warning(f"[MEM-TRACE] 发现 {len(large)} 个大分配热点 → {log_path}")
            self._mem_trace_snapshot = snap
        except Exception as e:
            logger.debug(f"[MEM-TRACE] skip: {e}")

    def _mem_snapshot(self, step_name: str, **extra):
        """
        在关键步骤捕获实时内存快照并记录日志。

        在 `run()` 每个关键过渡点调用，定位内存异常增长发生在哪个阶段。

        Args:
            step_name: 步骤标识（如 start, before_api_call, after_streaming,
                      before_tool_exec, after_tool_exec, end_iter）
            **extra: 步骤特定的附加度量（如 msg_count=N, tool_count=N）

        日志格式（info级别，可用 ` | findstr "[MEM]"` 过滤）：
            [MEM] step=before_api_call iter=3 rss=245.6MB (Δ+2.3MB) \
                  chunks=150 msg=5 api_cache=10 tool_calls=2

        异常检测（warning级别）：
            - 单步 RSS 增长 > 50MB
            - _response_chunks 数量 > 10000 且未被清理
            - 总数据结构估算大小 > 500MB
        """
        if not self._mem_diag_enabled:
            return

        if _HAS_PSUTIL:
            try:
                rss_mb = _psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
            except Exception:
                rss_mb = 0.0
        else:
            rss_mb = 0.0

        # 计算 RSS 增量（与上次快照相比）
        if self._mem_last_rss > 0 and rss_mb > 0:
            delta = rss_mb - self._mem_last_rss
            delta_str = f"{delta:+.1f}" if abs(delta) >= 0.1 else "~0"
        else:
            delta_str = "init"

        # 估算 _response_chunks 的字符串总长度
        chunks_total_len = sum(len(c) for c in self._response_chunks)

        # 基础度量
        parts = [
            f"step={step_name}",
            f"iter={self._mem_diag_iter_count}",
            f"rss={rss_mb:.1f}MB",
            f"Δ={delta_str}MB",
            f"chunks#{len(self._response_chunks)}",
            f"chunks~{chunks_total_len // 1024}KB" if chunks_total_len > 1024 else f"chunks~{chunks_total_len}B",
            f"blocks#{len(self._response_content_blocks)}",
            f"calls@{len(self._current_tool_calls)}",
            f"buf@{len(self._tool_calls_buffer)}",
        ]

        if extra:
            for k, v in extra.items():
                if isinstance(v, float):
                    parts.append(f"{k}={v:.1f}")
                else:
                    parts.append(f"{k}={v}")

        logger.info(f"[MEM] {' '.join(parts)}")

        # === 异常检测 ===
        issues = []
        if rss_mb > 0 and self._mem_last_rss > 0:
            delta_abs = abs(delta) if isinstance(delta, (int, float)) else 0
            if rss_mb - self._mem_last_rss > 50:
                issues.append(f"单步RSS增长>{rss_mb - self._mem_last_rss:.0f}MB")
        if len(self._response_chunks) > 10000:
            issues.append(f"_response_chunks={len(self._response_chunks)}>10000")
        if chunks_total_len > 50 * 1024 * 1024:  # 50MB
            issues.append(f"_response_chunks累计文本>{chunks_total_len // 1024 // 1024}MB")

        if issues:
            logger.warning(f"[MEM-LEAK] step={step_name}: {'; '.join(issues)}")

        # 异常大分配时触发 tracemalloc 深度追踪（MEM_TRACE=1）
        if issues and step_name in ("after_api_call", "after_tool_exec"):
            self._mem_take_trace()
        elif step_name == "before_api_call":
            # 在每个 API 调用前采集基准快照
            self._mem_take_trace()

        self._mem_last_rss = rss_mb
        self._mem_diag_logged = True

    def _sync_state_from_state(self):
        """
        将 self._state 的值同步到旧的实例属性名（向后兼容）。
        确保写 self._is_cancelled = True 之类的旧代码仍然能读/写到正确值。
        """
        s = self._state
        self.full_response = s.response.full_response
        self._response_chunks = s.response.response_chunks
        self._is_cancelled = s.is_cancelled
        self._question_pending = s.permission.pending_question
        self._pending_answer = s.permission.pending_answer
        self._answer_event = s.permission.answer_event
        self._permission_pending = s.permission.pending_permission
        self._permission_approved = s.permission.permission_approved
        self._permission_cache = s.permission_cache
        self._previewed_tool_call_ids = s.tool_call.previewed_ids
        self._current_tool_calls = s.tool_call.current_calls
        self._tool_calls_buffer = s.tool_call.calls_buffer
        self._reasoning_content = s.response.reasoning_content
        self._http_client = s.http.client
        self._reasoning_chunks = s.response.reasoning_chunks
        self._response_content_blocks = s.response.content_blocks
        self._tool_execution_cancelled = s.tool_call.execution_cancelled
        self._waiting_tool_params = s.tool_call.waiting_params
        self._last_progress_len = s.response.last_progress_len
        self._last_compaction_state = s.compaction.last_state
        self._current_session_messages = s.session.current_messages
        self._last_usage = s.session.last_usage
        self._accumulated_tokens = s.session.accumulated_tokens
        self._compactor = s.compactor
        self._compaction_cache = s.compaction.cache
        self._api_messages_cache = s.api_cache.cache
        self._api_messages_built = s.api_cache.built
        self._event_bus = s.event_bus

    def _sync_state(self):
        """
        将当前实例属性值同步回 self._state。
        调用时机：cancel(), cleanup(), _clear_pending_response_state()
        """
        s = self._state
        s.response.full_response = self.full_response
        s.is_cancelled = self._is_cancelled
        s.permission.pending_question = self._question_pending
        s.permission.pending_answer = self._pending_answer
        s.permission.pending_permission = self._permission_pending
        s.permission.permission_approved = self._permission_approved
        s.tool_call.current_calls = self._current_tool_calls
        s.tool_call.calls_buffer = self._tool_calls_buffer
        s.response.reasoning_content = self._reasoning_content
        s.http.client = self._http_client
        s.tool_call.execution_cancelled = self._tool_execution_cancelled
        s.tool_call.waiting_params = self._waiting_tool_params
        s.session.current_messages = self._current_session_messages
        s.session.last_usage = self._last_usage
        s.session.accumulated_tokens = self._accumulated_tokens

    def _build_api_messages_cache(self) -> List[Dict[str, Any]]:
        """
        构建 API 消息缓存。
        只在首次调用时处理所有消息，之后增量追加。

        Returns:
            转换后的 API 消息列表
        """
        if self._api_messages_cache is not None:
            return self._api_messages_cache

        # 首次构建：处理所有历史消息
        self._api_messages_cache = messages_to_api(self.messages)
        self._api_messages_built = True
        return self._api_messages_cache

    def _append_to_api_cache(self, new_messages: List[Dict[str, Any]]) -> None:
        """
        将新消息追加到 API 缓存。
        只转换新消息并追加，避免重新处理整个列表。

        Args:
            new_messages: 新增的消息列表
        """
        if self._api_messages_cache is None:
            self._api_messages_cache = messages_to_api(new_messages)
            return

        # 只转换新消息并追加
        for msg in new_messages:
            api_msg = to_api_message(msg)
            if api_msg:
                if api_msg.get("role") == "user" and not api_msg.get("content"):
                    continue
                self._api_messages_cache.append(api_msg)

    @property
    def event_bus(self) -> WorkerEventBus:
        """获取事件总线实例"""
        return self._event_bus

    def _emit_via_event_bus(self, event: WorkerEvent, *args, **kwargs) -> None:
        """通过事件总线发射事件

        推荐使用此方法替代 _emit_with_callback。
        事件总线会自动将事件分发给所有订阅者。

        Args:
            event: 事件类型
            *args, **kwargs: 事件数据
        """
        # 修复：cleanup() 会把 self._event_bus 置为 None 以断开闭包引用，
        # 而 ThreadPoolExecutor 子线程仍可能在并行工具执行结束后调用本方法，
        # 此处需做 None 保护，避免 'NoneType' object has no attribute 'emit'。
        bus = self._event_bus
        if bus is None:
            return
        bus.emit(event, *args, **kwargs)

    def _emit_with_callback(self, signal_name: str, signal, *args) -> None:
        """发射信号并尝试直接回调（已废弃，推荐使用 _emit_via_event_bus）

        兼容旧接口，内部使用事件总线分发事件。

        Args:
            signal_name: 信号名（用于查找直接回调）
            signal: Qt 信号对象（保留用于向后兼容）
            *args: 传递给回调/信号的参数
        """
        try:
            # 映射 signal_name 到 WorkerEvent
            event = self._signal_name_to_event(signal_name)
            if event:
                self._emit_via_event_bus(event, *args)

            # 向后兼容：仍然发射 PyQt Signal（UI 层依赖）
            # 注意：从 ThreadPoolExecutor 线程访问 pyqtSignal 时，
            # 某些 PyQt5 版本可能返回 None，所以需要保护性发射。
            if signal is not None:
                try:
                    signal.emit(*args)
                except (AttributeError, RuntimeError, TypeError) as e:
                    logger.debug(f"[Signal] PyQt信号发射失败 {signal_name}: {e}")
        except Exception as e:
            # 兜底：cleanup() 后子线程调用本方法可能命中各类 None 资源，
            # 这里吞掉而非向上抛，避免阻断并行工具结果的处理路径。
            logger.debug(f"[Signal] _emit_with_callback 兜底 {signal_name}: {e}")

    def _signal_name_to_event(self, signal_name: str) -> Optional[WorkerEvent]:
        """将 signal name 映射到 WorkerEvent"""
        mapping = {
            "content_received": WorkerEvent.CONTENT_RECEIVED,
            "reasoning_content_received": WorkerEvent.REASONING_RECEIVED,
            "finished_with_content": WorkerEvent.FINISHED_WITH_CONTENT,
            "finished_with_messages": WorkerEvent.FINISHED_WITH_MESSAGES,
            "compaction_status_changed": WorkerEvent.COMPACTION_STATUS,
            "tool_call_started": WorkerEvent.TOOL_CALL_STARTED,
            "tool_args_updated": WorkerEvent.TOOL_CALL_STREAM,
            "tool_result_received": WorkerEvent.TOOL_RESULT_RECEIVED,
            "question_asked": WorkerEvent.QUESTION_ASKED,
            "permission_approval_requested": WorkerEvent.PERMISSION_REQUESTED,
            "error_occurred": WorkerEvent.ERROR,
        }
        return mapping.get(signal_name)

    def cancel(self):
        self._state.is_cancelled = True
        self._state.tool_call.execution_cancelled = True
        self._is_cancelled = True
        self._tool_execution_cancelled = True
        self._answer_event.set()
        # 注意：不清除 _question_pending，由 cleanup() 统一清理。
        # cancel() 已设置 _is_cancelled=True，worker 线程会在 wait 循环后
        # 通过 if self._is_cancelled: return 提前返回，不访问 _question_pending。
        # 若此处清除 _question_pending，可能和 cleanup() 重置 _is_cancelled
        # 形成竞态：worker 读到 _is_cancelled=False 但 _question_pending=None 导致崩溃。
        if self._permission_pending:
            self._permission_pending = None
            self._state.permission.pending_permission = None

        # 🛡️ 请求 Qt 线程中断（替代 terminate() 的安全机制）
        # 设置 isInterruptionRequested() 标志供 run() 检查，
        # 线程在安全的检查点自行退出，避免 OS 级强杀。
        self.requestInterruption()

        # 🛡️ 关闭流式响应连接，立即中断 worker 线程的 for chunk in response: 等待
        if self._current_response is not None:
            try:
                self._current_response.close()
            except Exception:
                pass
            self._current_response = None

    def get_interrupted_messages(self) -> List[Dict]:
        """
        获取被中断时的消息快照。

        仅保存 worker 本次新增的消息（助理回复 + 工具结果 + 局部流式响应）。
        原有会话消息（含用户输入）已在外部 session 中持久化，不需要重复保存。

        策略：
        1. 以 _current_session_messages 为基线（含原始会话消息 + 已完成的工具迭代消息）
        2. 叠加当前流式生成的局部响应（_build_response_message_sequence）

        Returns:
            整合后的消息列表
        """
        snapshot = list(self._current_session_messages or self.session_messages or [])
        partial_sequence = self._build_response_message_sequence()
        if partial_sequence:
            for msg in partial_sequence:
                if msg not in snapshot:
                    snapshot.append(msg)
        return consolidate_messages(snapshot)

    def _clear_pending_response_state(self):
        """
        清理单轮对话结束后的中间状态。
        在每次 API 调用前和工具执行完成后调用，释放内存。
        """
        # 使用 ChatWorkerState 清理
        self._state.reset_pending_response_state()
        self._sync_state_from_state()

    def _get_reasoning_content(self) -> str:
        """获取当前的 reasoning_content（从累积的 chunks 合成）"""
        if self._reasoning_chunks:
            return ''.join(self._reasoning_chunks)
        return self._reasoning_content

    def cleanup(self):
        """
        彻底清理 worker 的所有缓存数据，防止内存泄漏。
        应该在对话结束后调用。
        """
        # 清理消息引用（非 state 管理的）
        self.messages = []
        self.session_messages = []
        self._current_api_messages = []
        self._current_system_prompt = ''

        # 使用 ChatWorkerState 清理所有状态
        self._state.full_cleanup()
        self._sync_state_from_state()

        # 清理问题/回答状态
        self._pending_answer = None
        self._question_pending = None

        # 清理会话缓存
        self._current_session_messages = []

        # 清理 HTTP 客户端和流式响应引用
        self._http_client = None
        self._cached_api_config = None
        self._current_response = None

        # 🔧 修复：清空 EventBus 订阅者，防止 handler 闭包引用残留
        # 如果不清理，EventBus 的 _handlers 字典中保留所有订阅的 lambda 闭包，
        # 这些闭包捕获了 worker 自身的引用，形成循环引用阻止 GC。
        try:
            if hasattr(self, '_event_bus') and self._event_bus is not None:
                self._event_bus.clear()
                self._event_bus = None
        except Exception:
            pass
        if hasattr(self, '_state') and self._state is not None:
            self._state.event_bus = None

        # 🔧 修复：主动压缩进程堆，让 Python 分配器归还空闲 arena 给 OS
        # 防止 RSS 在多次对话间持续增长不下降
        _compact_process_heap()

    # ========== 缓存追踪方法 ==========

    def get_cache_stats(self) -> 'AggregatedCacheStats':
        """获取缓存追踪统计"""
        return self._cache_tracker.get_session_stats()

    def get_cache_hit_rate(self) -> float:
        """获取当前缓存命中率"""
        return self._cache_tracker.get_current_hit_rate()

    def get_cache_hit_rate_display(self) -> str:
        """获取格式化的命中率显示"""
        return self._cache_tracker.get_hit_rate_display()

    def reset_cache_stats(self):
        """重置缓存统计"""
        self._cache_tracker.start_session()

    def get_cache_stats_summary(self) -> str:
        """获取缓存统计摘要"""
        return self._cache_tracker.summary()

    def _get_http_client(self) -> Any:
        """
        获取或创建复用的 HTTP 客户端。
        避免每次 API 调用都创建新的客户端。
        """
        if self._http_client is None:
            self._http_client = OpenAI(
                api_key=self.llm_config.get("API_KEY", "").strip(),
                base_url=self.llm_config.get("API_URL"),
                timeout=httpx.Timeout(600.0, connect=60.0),
            )
        return self._http_client

    def _build_api_request_kwargs(self) -> Dict[str, Any]:
        """
        预构建 API 请求参数，避免每次调用都重复处理。
        缓存结果，只在配置变化时重新构建。
        """
        # 检查是否需要更新缓存
        # 缓存键必须包含所有影响 extra_body 的参数，否则改思考模式等不会生效
        sig_parts = [
            str(self.llm_config.get(k, ""))
            for k in ("API_KEY", "API_URL", "模型名称",
                      "思考模式", "思考等级", "思考预算",
                      "温度", "top_p", "最大Token", "max_new_tokens")
        ]
        config_key = "|".join(sig_parts)

        if self._cached_api_config is not None and self._cached_api_config.get("_config_key") == config_key:
            # 缓存有效，返回基础配置（messages 和 tools 每次不同，需要单独设置）
            return {
                "model": self._cached_api_config["model"],
                "stream": self.stream,
                "extra_body": dict(self._cached_api_config.get("extra_body") or {}),
                "_auth_headers": self._cached_api_config.get("_auth_headers"),
                "_is_o1_model": self._cached_api_config.get("_is_o1_model"),
            }

        # 构建新的缓存
        api_key = self.llm_config.get("API_KEY", "").strip()
        base_url = self.llm_config.get("API_URL") or None
        model = str(self.llm_config.get("模型名称", "gpt-4o"))

        extra_body = {}

        skip_params = {"temperature", "top_p", "presence_penalty", "frequency_penalty"}
        if model and (model.startswith("o1") or model.startswith("o3")):
            skip_params.update({"temperature", "top_p"})

        for cn_key, value in self.llm_config.items():
            if cn_key in {"API_KEY", "API_URL", "API_BASE", "认证方式", "模型名称",
                          "系统提示", "启用技能",
                          "name", "provider_name", "config_id", "display_name",
                          "_suffix_index", "备注", "获取地址", "模型列表"}:
                continue
            if cn_key in QUOTA_EXCLUDE_KEYS:
                continue
            # 从 PARAM_SCHEMA 查找 API 参数名
            meta = PARAM_SCHEMA.get(cn_key, {})
            en_key = meta.get("api_param")
            if not en_key and _VALID_IDENTIFIER_PATTERN.match(cn_key):
                en_key = cn_key
            if not en_key or en_key in skip_params:
                continue
            if en_key in ["max_tokens"]:
                continue  # 单独处理
            extra_body[en_key] = value

        # 处理 max_tokens
        max_tokens = self.llm_config.get("最大Token")
        if max_tokens is not None:
            extra_body["max_tokens"] = self._cap_max_output_tokens(model, max_tokens)

        # 处理思考模式（通用逻辑，不再按 family 硬编码）
        thinking_mode = self.llm_config.get("思考模式")
        if thinking_mode is not None:
            # 优先从 MODEL_CAPABILITIES 获取 thinking_param，回退到 provider_profile
            caps = get_model_capabilities(model)
            t_param = None
            enable_value = "enabled"  # 大多数模型用 "enabled"
            if caps:
                t_param = caps.get("thinking_param")
                enable_value = caps.get("thinking_enable_value", "enabled")
            if not t_param:
                profile = get_provider_profile(self.llm_config)
                t_param = profile.get("thinking_param")

            if thinking_mode is True:
                if t_param == "thinking":
                    extra_body["thinking"] = {"type": enable_value}
                    # 用 thinking 控制的模型不支持同时传 reasoning_effort
                    extra_body.pop("reasoning_effort", None)
                    extra_body.pop("thinking_budget", None)
                elif t_param == "thinking_budget":
                    budget = self.llm_config.get("思考预算", 4096)
                    extra_body["thinking_budget"] = budget
                    # thinking_budget 型清理非 budget 参数
                    extra_body.pop("reasoning_effort", None)
                    extra_body.pop("thinking", None)
                elif t_param == "reasoning_effort":
                    # reasoning_effort 由 思考等级 的 api_param 映射自动流入
                    # 这里确保兜底值并清理冲突参数
                    if "reasoning_effort" not in extra_body:
                        extra_body["reasoning_effort"] = self.llm_config.get("思考等级", "medium")
                    extra_body.pop("thinking", None)
                    extra_body.pop("thinking_budget", None)
            else:  # False - 关闭思考
                # 显式告诉 API 不要思考（所有 t_param 类型都发此通用信号）
                extra_body["thinking"] = {"type": "disabled"}
                # 同时清理可能残留的其他思考参数
                extra_body.pop("thinking_budget", None)
                extra_body.pop("reasoning_effort", None)

            logger.debug(
                f"[Thinking] mode={thinking_mode}, t_param={t_param}, "
                f"extra_body_keys={[k for k in extra_body if k in ('thinking', 'thinking_budget', 'reasoning_effort')]}"
            )

        # 处理认证
        auth_headers = None
        auth_type = self.llm_config.get("认证方式", "bearer")
        if auth_type == "bce":
            import base64
            auth_str = f"{api_key}:{api_key}"
            b64_auth = base64.b64encode(auth_str.encode()).decode()
            auth_headers = {"Authorization": f"Basic {b64_auth}"}

        is_o1 = model.startswith("o1") or model.startswith("o3")

        self._cached_api_config = {
            "_config_key": config_key,
            "model": model,
            "extra_body": extra_body,
            "_auth_headers": auth_headers,
            "_is_o1_model": is_o1,
        }

        return {
            "model": model,
            "stream": self.stream,
            "extra_body": extra_body,
            "_auth_headers": auth_headers,
            "_is_o1_model": is_o1,
        }

    def provide_answer(self, answer: str):
        self._pending_answer = answer
        self._answer_event.set()

    def set_session_permission_cache(self, tool_name: str, allowed: bool = True):
        """设置会话级权限缓存（本次会话允许）"""
        if allowed:
            self._permission_cache.allow_session(tool_name)
        else:
            self._permission_cache.deny(tool_name)

    def approve_permission(self, tool_call_id: str, auto_allow: bool = False, session_allow: bool = False):
        if (
                self._permission_pending
                and self._permission_pending.get("tool_call_id") == tool_call_id
        ):
            tool_name = self._permission_pending.get("tool_name", "")
            if auto_allow:
                self._permission_cache.allow_round(tool_name)
            if session_allow:
                self._permission_cache.allow_session(tool_name)
            self._permission_approved = True
            self._permission_pending = None

    def deny_permission(self, tool_call_id: str):
        if (
                self._permission_pending
                and self._permission_pending.get("tool_call_id") == tool_call_id
        ):
            self._permission_approved = False
            self._permission_pending = None

    def run(self):
        try:
            current_messages = self.messages.copy()
            current_session_messages = list(self.session_messages)
            self._current_session_messages = list(current_session_messages)
            self._emit_compaction_status(self._last_compaction_state)
            self.full_response = ""
            self._reasoning_content = ""
            # 开始新对话时，清理所有中间状态，重建 API 消息缓存
            self._clear_pending_response_state()
            self._api_messages_cache = None  # 重置缓存，下次 API 调用时重建
            self._api_messages_built = False
            self._accumulated_tokens = 0  # 重置 token 累加，每个新的对话从零开始

            # 开始新对话时，清理 round 缓存，但保留 session 缓存
            self._permission_cache.clear_round()
            budget = self._compactor.get_budget(self.llm_config)

            # [MEM] 启动快照
            self._mem_snapshot("start", msg_count=len(current_messages), session_count=len(current_session_messages))

            while not self._is_cancelled:
                if self._is_cancelled:
                    return

                # 每次 API 调用前：1. 清理中间状态  2. 检查压缩
                self._clear_pending_response_state()

                # [MEM] API 调用前
                self._mem_snapshot("before_api_call",
                    msg_count=len(current_messages),
                    api_cache=len(self._api_messages_cache) if self._api_messages_cache else 0,
                    session_count=len(current_session_messages))

                # 使用 API 消息缓存（首次会重建，后续复用）
                tool_calls_found, tool_args_pending = self._make_api_call(current_messages, use_cache=True)
                if self._is_cancelled:
                    # 🛡️ 取消前保存已收到的 partial 响应，避免消息丢失
                    partial_sequence = self._build_response_message_sequence()
                    if partial_sequence:
                        current_messages.extend(partial_sequence)
                        current_session_messages.extend(partial_sequence)
                        self._current_session_messages = list(current_session_messages)
                        self.full_response = ''.join(self._response_chunks)
                        # 虽然信号可能已被断开，但事件总线仍可能接收
                        self._emit_with_callback("finished_with_messages", self.finished_with_messages,
                                                 current_session_messages)
                    return
                if tool_calls_found and tool_args_pending:
                    # 🛡️ continue 之前检查取消状态，否则 while 循环直接退出绕过保存
                    if self._is_cancelled:
                        partial_sequence = self._build_response_message_sequence()
                        if partial_sequence:
                            current_messages.extend(partial_sequence)
                            current_session_messages.extend(partial_sequence)
                            self._current_session_messages = list(current_session_messages)
                            self.full_response = ''.join(self._response_chunks)
                            self._emit_with_callback("finished_with_messages", self.finished_with_messages,
                                                     current_session_messages)
                        return
                    continue
                if self._is_cancelled:
                    # 🛡️ 同上，保存 partial 响应
                    partial_sequence = self._build_response_message_sequence()
                    if partial_sequence:
                        current_messages.extend(partial_sequence)
                        current_session_messages.extend(partial_sequence)
                        self._current_session_messages = list(current_session_messages)
                        self.full_response = ''.join(self._response_chunks)
                        self._emit_with_callback("finished_with_messages", self.finished_with_messages,
                                                 current_session_messages)
                    return

                # [MEM] API 返回后
                self._mem_snapshot("after_api_call",
                    tool_calls_found=tool_calls_found,
                    tool_args_pending=tool_args_pending,
                    resp_chunks=len(self._response_chunks),
                    resp_blocks=len(self._response_content_blocks),
                    msg_count=len(current_messages))

                if not tool_calls_found:
                    response_sequence = self._build_response_message_sequence()
                    current_messages.extend(response_sequence)
                    current_session_messages.extend(response_sequence)
                    self._current_session_messages = list(current_session_messages)
                    # 更新 API 消息缓存：追加响应消息
                    self._append_to_api_cache(response_sequence)
                    # 性能优化：在发送前才合成完整响应字符串
                    self.full_response = ''.join(self._response_chunks)
                    self._emit_with_callback("finished_with_messages", self.finished_with_messages,
                                             current_session_messages)
                    self._clear_pending_response_state()
                    self._emit_with_callback("finished_with_content", self.finished_with_content, self.full_response)
                    return

                # [MEM] 执行工具前
                tool_count = len(self._current_tool_calls) if self._current_tool_calls else 0
                self._mem_snapshot("before_tool_exec",
                    tool_count=tool_count,
                    msg_count=len(current_messages))

                tool_results = self._execute_all_tools()

                if tool_results is None:
                    # 提前捕获 _question_pending，避免 cancel+cleanup 竞态导致丢失引用
                    q = self._question_pending
                    self._answer_event.clear()
                    while self._pending_answer is None and not self._is_cancelled:
                        if self._answer_event.wait(timeout=1.0):
                            break

                    if self._is_cancelled:
                        return

                    # 先构造 question_result，再传入 _build_response_message_sequence，
                    # 避免 tool_results=None 被误判为"取消中断"场景而清空 tool_calls
                    # ⚠️ arguments 必须保留原始 questions，否则渲染时折叠预览/展开表格都看不到参数
                    question_questions = q.get("questions", [])
                    question_args = {"questions": question_questions}
                    question_result = {
                        "role": "tool",
                        "tool_call_id": q["tool_call_id"],
                        "name": "question",
                        "arguments": question_args,
                        "content": self._pending_answer,
                        "success": True,
                    }
                    # 发射 tool_result_received，让 UI 在助理卡片中渲染可折叠工具块
                    result_obj = {"success": True, "content": self._pending_answer}
                    self._emit_with_callback("tool_result_received", self.tool_result_received,
                                             q["tool_call_id"], "question", question_args, result_obj)
                    response_sequence = self._build_response_message_sequence([question_result])
                    current_messages.extend(response_sequence)
                    current_session_messages.extend(response_sequence)
                    self._current_session_messages = list(current_session_messages)
                    # 更新 API 消息缓存
                    self._append_to_api_cache(response_sequence)
                    self._emit_with_callback("finished_with_messages", self.finished_with_messages,
                                             current_session_messages)
                    self._question_pending = None
                    self._pending_answer = None
                    self._answer_event.clear()
                    continue

                # [MEM] 工具执行完成
                result_count = len(tool_results) if tool_results else 0
                tool_names = [r.get("name", "?") for r in (tool_results or [])[:3]]
                tool_result_sizes = sum(len(str(r.get("content", ""))) for r in (tool_results or []))
                self._mem_snapshot("after_tool_exec",
                    result_count=result_count,
                    tool_names=",".join(tool_names),
                    result_sizes=f"{tool_result_sizes // 1024}KB" if tool_result_sizes > 1024 else f"{tool_result_sizes}B",
                    msg_count=len(current_messages))

                # ========== 工具结果持久化 (入口管控) ==========
                # 借鉴 Claude Code: 单结果 > 50K 字符 / 消息级 > 200K 字符 -> 落盘
                # 在 ToolExecutor 之后、消息拼接之前执行, 保护 Prompt Cache 前缀稳定
                # 完全无 LLM API 调用, 失败时回退保留原结果
                try:
                    persister = self._get_persister()
                    if persister and tool_results:
                        tool_results, persist_stats = persister.process(tool_results)
                        self._last_persist_stats = persist_stats.to_dict()
                except Exception as e:
                    logger.exception(f"[Persist] 持久化失败, 保留原结果: {e}")
                    self._last_persist_stats = None

                response_sequence = self._build_response_message_sequence(tool_results)
                current_messages.extend(response_sequence)
                current_session_messages.extend(response_sequence)
                self._current_session_messages = list(current_session_messages)

                # [MEM] 消息构建后
                self._mem_snapshot("after_build_msg",
                    msg_count=len(current_messages),
                    session_count=len(current_session_messages),
                    api_cache=len(self._api_messages_cache) if self._api_messages_cache else 0)

                # ========== 工具迭代中压缩 ==========
                # 在每次 API 调用前检查是否需要压缩
                if self._compactor.should_compact(current_messages, budget):
                    system_message = current_messages.pop(0)
                    compacted, state, cache = self._compactor.compact(
                        current_messages,
                        budget,
                        existing_cache=None,
                        allow_llm_summary=False,  # 工具迭代中只用启发式，避免嵌套 LLM 调用
                    )
                    if compacted != current_messages:
                        current_messages = compacted
                        self._last_compaction_state = state
                        self._compaction_cache = cache
                        self._emit_compaction_status(state)
                    # 修复：重新插入 system_message，否则下一轮 API 调用会丢失系统提示
                    current_messages.insert(0, system_message)
                    # 修复：始终更新 API 缓存以匹配 current_messages（含 system）
                    # 注意：需要通过 messages_to_api() 转换格式，否则后续 API 调用读到内部格式对象
                    # 🛡️ 先清理 orphan，再设缓存，避免缓存带脏数据
                    current_messages, _ = self._fix_tool_result_order(current_messages)
                    self._api_messages_cache = messages_to_api(current_messages)
                    # 注意：current_session_messages 故意不做同步压缩。
                    # 它的增长会在 worker 结束时由 _on_messages_updated 的
                    # preserve_compaction=False 清空缓存，下轮发送时由 ContextBudgetAllocator 统一压缩。
                else:
                    # 更新 API 消息缓存
                    self._append_to_api_cache(response_sequence)
                self._emit_with_callback("finished_with_messages", self.finished_with_messages,
                                         current_session_messages)

                # ========== 迭代结束：内存快照 ==========
                self._mem_diag_iter_count += 1
                _was_compacted = (  # 检测本轮是否执行了压缩
                    'compacted' in locals()
                    and locals().get('compacted') is not None
                )
                self._mem_snapshot("end_iter",
                    msg_count=len(current_messages),
                    session_count=len(current_session_messages),
                    api_cache=len(self._api_messages_cache) if self._api_messages_cache else 0,
                    compacted="yes" if _was_compacted else "no")
                # 每 3 轮触发一次 GC，帮助回收循环引用
                # 🔧 修复：仅在 MEM_DIAG 启用时才执行 gc.collect()，避免 stop-the-world GC 阻塞 UI
                if self._mem_diag_enabled and self._mem_diag_iter_count % 3 == 0:
                    before_gc = len(gc.get_objects())
                    gc.collect()
                    after_gc = len(gc.get_objects())
                    freed = before_gc - after_gc
                    if freed > 1000:
                        logger.debug(f"[MEM] GC后释放 {freed} 个对象")

                # 性能优化：移除后台线程中的 processEvents() 和 sleep
                # 原因：processEvents() 设计用于主线程，在后台线程调用会导致：
                # 1. 信号丢失或延迟
                # 2. UI 状态不一致
                # 3. 可能的死锁
                # 如果需要 UI 响应，应使用信号-槽机制而非强制事件处理
                # time.sleep(0.01)

        except Exception as e:
            logger.exception("请求失败!")
            # 🔧 异常时保存已生成的部分消息到会话
            try:
                partial_sequence = self._build_response_message_sequence()
                if partial_sequence:
                    current_session_messages.extend(partial_sequence)
                self._current_session_messages = list(current_session_messages)
                # 只发射 finished_with_messages（保存消息到会话），不发射 finished_with_content
                # （避免 UI 将其视为正常完成）
                self._emit_with_callback("finished_with_messages", self.finished_with_messages,
                                         current_session_messages)
            except Exception as save_err:
                logger.warning(f"[ChatWorker] Failed to save partial messages on error: {save_err}")
            self._handle_error(e)
        finally:
            # 工具执行完成后，清理 round 缓存（为下一轮 API 调用做准备）
            self._permission_cache.clear_round()

    def _build_response_message_sequence(self, tool_results=None) -> List[Dict]:
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        model_name = str(self.llm_config.get("模型名称", "") or "")

        # 性能优化：缓存 reasoning_content，避免重复 join
        reasoning_content = self._get_reasoning_content()

        tool_call_map = {}
        # 从 _current_tool_calls 字典获取 tool_calls
        current_tcs = self._current_tool_calls
        if current_tcs:
            for tc in current_tcs.values():
                if not isinstance(tc, dict):
                    continue
                tool_call_id = str(tc.get("id") or "")
                if not tool_call_id:
                    continue
                function = tc.get("function") or {}
                normalized_tc = {
                    "id": tool_call_id,
                    "type": tc.get("type", "function"),
                    "function": {
                        "name": function.get("name"),
                        "arguments": function.get("arguments", "{}"),
                    },
                }
                tool_call_map[tool_call_id] = normalized_tc

        # 从 _tool_calls_buffer 获取未解析完成的 tool_calls
        tool_calls_buffer = self._tool_calls_buffer
        if tool_calls_buffer:
            for tc_id, buffer in tool_calls_buffer.items():
                if tc_id in tool_call_map:
                    continue
                function = buffer.get("function") or {}
                tool_call_map[tc_id] = {
                    "id": tc_id,
                    "type": buffer.get("type", "function"),
                    "function": {
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments", "{}"),
                    },
                }

        # 也从 tool_results 中的 _tool_call 字段获取
        if tool_results:
            for item in tool_results:
                if isinstance(item, dict) and "tool_call_id" in item:
                    tc_id = item["tool_call_id"]
                    if tc_id and tc_id not in tool_call_map:
                        tool_call_map[tc_id] = {
                            "id": tc_id,
                            "type": "function",
                            "function": {
                                "name": item.get("name", ""),
                                "arguments": item.get("arguments", "{}"),
                            },
                        }

        tool_result_map = {}
        if tool_results:
            for item in tool_results:
                if not isinstance(item, dict):
                    continue
                tool_call_id = str(item.get("tool_call_id") or "")
                if not tool_call_id:
                    continue

                tool_result_map[tool_call_id] = {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": item.get("name", "tool"),
                    "arguments": item.get("arguments", {}),
                    "content": item.get("content", ""),
                    "success": item.get("success", True),
                    "round_id": item.get("round_id"),
                    "timestamp": item.get("timestamp", now_ts),
                    "diff": item.get("diff"),
                    "anchors": item.get("anchors"),
                    "echarts": item.get("echarts"),
                }

        # 预防性修复：过滤掉没有对应 tool 结果的 tool_call
        # 只有有结果的 tool_call 才能发送给 API，避免用户中断时产生 2013 错误
        if tool_result_map:
            # 只有有结果的 tool_call 才能发送给 API，避免用户中断时产生 2013 错误
            valid_tc_ids = set(tool_result_map.keys())
            filtered_tool_call_map = {}
            for tc_id, tc in tool_call_map.items():
                if tc_id in valid_tc_ids:
                    filtered_tool_call_map[tc_id] = tc
                else:
                    logger.warning(f"[ToolCall预防] 过滤了无结果的 tool_call: {tc_id[:20]}...")
            tool_call_map = filtered_tool_call_map

        sequence: List[Dict] = []
        pending_text_blocks = []
        response_blocks = self._response_content_blocks
        if response_blocks:
            for block in response_blocks:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text = str(block.get("text", ""))
                    if text:
                        pending_text_blocks = append_text_block(pending_text_blocks, text)
                    continue

                if block_type != "tool_call_marker":
                    continue

                tool_call_id = str(block.get("tool_call_id") or "")
                # 在取消中断场景下，tool_call_marker 已被过滤掉，不会走到这里
                assistant_msg: Dict[str, Any] = {
                    "role": "assistant",
                    "timestamp": now_ts,
                }
                if pending_text_blocks:
                    assistant_msg["content"] = pending_text_blocks[0].get("text")
                tool_call = tool_call_map.get(tool_call_id)
                if tool_call:
                    assistant_msg["tool_calls"] = [tool_call]
                if reasoning_content:
                    assistant_msg["reasoning_content"] = reasoning_content
                if model_name:
                    assistant_msg["model_name"] = model_name
                if assistant_msg.get("content") or assistant_msg.get("tool_calls"):
                    sequence.append(assistant_msg)

                pending_text_blocks = []
                tool_result = tool_result_map.get(tool_call_id)
                if tool_result:
                    sequence.append(tool_result)

        # 处理没有对应 marker 的 tool_result
        for tool_call_id, tool_result in tool_result_map.items():
            already_added = False
            for block in sequence:
                if block.get("role") == "tool" and block.get("tool_call_id") == tool_call_id:
                    already_added = True
                    break
            if not already_added:
                assistant_msg: Dict[str, Any] = {
                    "role": "assistant",
                    "timestamp": now_ts,
                }
                tool_call = tool_call_map.get(tool_call_id)
                if tool_call:
                    assistant_msg["tool_calls"] = [tool_call]
                if reasoning_content:
                    assistant_msg["reasoning_content"] = reasoning_content
                if model_name:
                    assistant_msg["model_name"] = model_name
                if assistant_msg.get("tool_calls"):
                    sequence.append(assistant_msg)
                sequence.append(tool_result)

        if pending_text_blocks:
            assistant_msg = {
                "role": "assistant",
                "content": pending_text_blocks[0].get("text"),
                "timestamp": now_ts,
            }
            if reasoning_content:
                assistant_msg["reasoning_content"] = reasoning_content
            if model_name:
                assistant_msg["model_name"] = model_name
            sequence.append(assistant_msg)
        elif not sequence and self.full_response:
            assistant_msg = {
                "role": "assistant",
                "content": append_text_block([], self.full_response)[0].get("text"),
                "timestamp": now_ts,
            }
            if reasoning_content:
                assistant_msg["reasoning_content"] = reasoning_content
            if model_name:
                assistant_msg["model_name"] = model_name
            sequence.append(assistant_msg)
        elif not sequence and not response_blocks:
            empty_msg = {"role": "assistant", "content": [], "timestamp": now_ts}
            if model_name:
                empty_msg["model_name"] = model_name
            sequence.append(empty_msg)

        return sequence

    def _emit_compaction_status(self, state: Dict):
        # 性能优化：减少重复的 dict.get 调用
        state = state or {}
        normalized = {
            "active": bool(state.get("active", False)),
            "source": state.get("source", "worker"),
            "kind": state.get("kind", ""),
            "original_count": int(state.get("original_count", 0) or 0),
            "summarized_count": int(state.get("summarized_count", 0) or 0),
            "kept_count": int(state.get("kept_count", 0) or 0),
            "summary_count": int(state.get("summary_count", 0) or 0),
            "note": str(state.get("note", "") or ""),
        }
        if normalized == self._last_compaction_state:
            return
        self._last_compaction_state = normalized
        self._emit_with_callback("compaction_status_changed", self.compaction_status_changed, dict(normalized))

    def _fix_tool_result_order(self, messages: List[Dict]) -> tuple[List[Dict], bool]:
        """
        修复消息列表中 tool result 顺序问题。

        处理 API 格式消息（tool 消息只有 role, tool_call_id, name, content）。
        规则：每个 tool 消息的 tool_call_id 必须与之前的 assistant 消息的 tool_calls 中的 id 匹配。

        主要问题：
        1. 用户中断时：assistant 消息已包含 tool_calls，但对应的 tool 结果还没有被追加
        2. 重复的 tool_call_id：之前的修复尝试可能累积了重复的 tool_call_id

        修复策略：
        1. 收集所有 tool 消息的 tool_call_id（这些是"有效的"）
        2. 对每个 assistant 消息，只保留那些在 tool 消息中存在对应结果的 tool_call
        3. 如果所有 tool_call 都没有对应结果（用户中断场景），移除 tool_calls 字段
        4. 如果有重复的 tool_call_id，只保留第一个

        Returns:
            (修复后的消息列表, 是否进行了修复)
        """
        fixed_messages: List[Dict] = []
        modified = False

        # 第一步：收集所有 tool 消息中的 tool_call_id（这些是"有效的"）
        valid_tool_call_ids: set = set()
        for msg in messages:
            if msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id", "")
                if tc_id:
                    valid_tool_call_ids.add(tc_id)

        logger.warning(f"[ToolCall修复] 有效 tool_call_ids: {len(valid_tool_call_ids)} 个")

        # 如果没有任何 tool 消息，说明是用户中断场景，但没有累积的工具结果
        # 这种情况下直接返回无需修复
        if not valid_tool_call_ids:
            # 检查是否有 assistant 消息包含 tool_calls
            has_tool_calls = any(
                msg.get("role") == "assistant" and msg.get("tool_calls")
                for msg in messages
            )
            if has_tool_calls:
                logger.warning("[ToolCall修复] 检测到用户中断场景：assistant 有 tool_calls 但无任何 tool 结果")
                # 移除所有 assistant 消息中的 tool_calls
                for msg in messages:
                    if msg.get("role") == "assistant":
                        if msg.get("tool_calls"):
                            msg.pop("tool_calls", None)
                            # 确保 content 不为 None，避免 API 报 "content or tool_calls must be set"
                            if msg.get("content") is None:
                                msg["content"] = ""
                            modified = True
                            logger.info("[ToolCall修复] 已移除中断时的 tool_calls")
                return messages, modified
            return messages, False

        # 第二步：遍历每个 assistant 消息，修复 tool_calls
        for msg in messages:
            if msg.get("role") != "assistant":
                fixed_messages.append(msg)
                continue

            # 深拷贝，避免修改原消息
            fixed_msg = dict(msg)
            tool_calls = fixed_msg.get("tool_calls") or []

            if not tool_calls:
                fixed_messages.append(fixed_msg)
                continue

            # 去重并过滤：只保留有对应 tool 结果的 tool_call
            seen_ids: set = set()
            new_tool_calls: List[Dict] = []
            removed_count = 0

            for tc in tool_calls:
                tc_id = tc.get("id", "")

                # 检查重复
                if tc_id in seen_ids:
                    logger.warning(f"[ToolCall修复] 发现重复 tool_call_id: {tc_id[:20]}...，已移除")
                    modified = True
                    removed_count += 1
                    continue

                # 检查是否有对应的 tool 结果
                if tc_id and tc_id not in valid_tool_call_ids:
                    logger.warning(f"[ToolCall修复] tool_call {tc_id[:20]}... 无对应 tool 结果，已移除")
                    modified = True
                    removed_count += 1
                    continue

                seen_ids.add(tc_id)
                new_tool_calls.append(tc)

            if new_tool_calls:
                fixed_msg["tool_calls"] = new_tool_calls
            else:
                # 所有 tool_call 都没有对应结果，移除 tool_calls 字段
                fixed_msg.pop("tool_calls", None)
                # 确保 content 不为 None，避免 API 报 "content or tool_calls must be set"
                if fixed_msg.get("content") is None:
                    fixed_msg["content"] = ""
                logger.info("[ToolCall修复] 所有 tool_call 均无对应结果，已移除 tool_calls 字段")

            fixed_messages.append(fixed_msg)

        return fixed_messages, modified

    def _try_recover_tool_arguments(self, messages: List[Dict]) -> Optional[List[Dict]]:
        """
        尝试从历史消息中恢复 tool_calls 的参数。

        当检测到 "Missing required arguments" 错误时调用。
        检查是否有 tool 结果被错误处理导致参数丢失。

        Returns:
            修复后的消息列表，如果无法修复则返回 None
        """
        try:
            # 查找所有 assistant 消息中的 tool_calls
            tool_calls_by_content_hash: Dict[str, Dict] = {}

            for msg in messages:
                if msg.get("role") == "assistant":
                    tool_calls = msg.get("tool_calls") or []
                    for tc in tool_calls:
                        function = tc.get("function", {}) or {}
                        arguments = function.get("arguments", "{}")
                        # 使用 arguments 的 hash 作为键（用于匹配）
                        args_hash = str(arguments)[:100]
                        if args_hash:
                            tool_calls_by_content_hash[args_hash] = tc

            # 检查是否有 tool 消息缺少必要的参数信息
            # 如果有对应的 assistant 消息中有完整的 tool_calls，说明参数可能被错误处理了
            if not tool_calls_by_content_hash:
                logger.warning("[ToolCall恢复] 未找到任何 tool_calls，无法恢复参数")
                return None

            # 返回 None 表示无法自动恢复，但记录了尝试
            logger.info(f"[ToolCall恢复] 找到 {len(tool_calls_by_content_hash)} 个 tool_calls 用于参数匹配")
            return None

        except Exception as e:
            logger.warning(f"[ToolCall恢复] 尝试恢复工具参数时出错: {e}")
            return None

    def _make_api_call(self, messages: List[Dict], use_cache: bool = True) -> (bool, bool):
        """
        发起 API 调用。

        性能优化：
        1. 使用缓存的 API 消息，避免每次都重新处理所有消息
        2. 使用缓存的 HTTP 客户端，避免每次都创建新客户端
        3. 预构建 API 参数，避免每次都重复处理
        """
        # 🛡️ 清除旧响应引用，确保 cancel() 不关闭过期连接
        self._current_response = None

        # 性能优化：使用缓存的 API 消息
        if use_cache and self._api_messages_cache is not None:
            sanitized = self._api_messages_cache
        else:
            sanitized = messages_to_api(messages)
            if use_cache:
                self._api_messages_cache = sanitized
                self._api_messages_built = True

        # 性能优化：使用预构建的 API 参数
        cached_config = self._build_api_request_kwargs()

        req_kwargs: Dict[str, Any] = {
            "model": cached_config["model"],
            "messages": sanitized,
            "stream": cached_config["stream"],
            # parallel_tool_calls 不传：OpenAI 默认 True，非 OpenAI 提供商可能不支持（422 报错）
        }
        # 添加 extra_body
        if cached_config.get("extra_body"):
            req_kwargs["extra_body"] = cached_config["extra_body"]

        # 添加认证头
        if cached_config.get("_auth_headers"):
            req_kwargs["extra_headers"] = cached_config["_auth_headers"]

        # 添加 tools
        if self.tools:
            req_kwargs["tools"] = self.tools

        # 处理 o1 模型
        if cached_config.get("_is_o1_model"):
            req_kwargs.pop("stream", None)
            self.stream = False

        # 性能优化：使用复用的 HTTP 客户端
        client = self._get_http_client()

        max_retries = 15
        retry_delay = 5
        last_error = None

        for attempt in range(max_retries):
            # 用户取消时立即退出重试循环
            if self._is_cancelled:
                logger.info("[API] 重试被用户取消")
                return None, None
            try:
                response = client.chat.completions.create(**req_kwargs)
                if attempt > 0:
                    self.retry_resolved.emit()
                break
            except BadRequestError as e:
                error_str = str(e)
                # 检测 tool call result 错误码 2013
                is_tool_call_order_error = (
                        "2013" in error_str or
                        "tool call result does not follow tool call" in error_str.lower() or
                        "tool_calls" in error_str.lower()
                )

                if is_tool_call_order_error and attempt < max_retries - 1:
                    # 自动修复 tool result 顺序问题
                    logger.warning(f"[API] 检测到 tool call result 顺序错误 (2013)，尝试自动修复...")
                    fixed_messages, was_fixed = self._fix_tool_result_order(req_kwargs["messages"])

                    if was_fixed:
                        fixed_sanitized = messages_to_api(fixed_messages)
                        req_kwargs["messages"] = fixed_sanitized
                        # 更新 API 消息缓存，修复结果持久化，避免下一轮迭代重复修复
                        if use_cache:
                            self._api_messages_cache = fixed_sanitized
                        # 🛡️ 同步修复源头 current_messages（in-place），彻底固化修复结果
                        # _fix_tool_result_order 只读 role/tool_call_id/tool_calls，内外格式兼容
                        fixed_source, _ = self._fix_tool_result_order(messages)
                        if fixed_source is not messages:
                            messages[:] = fixed_source
                        logger.warning(f"[API] 已修复消息顺序，已同步源头，重试 (attempt {attempt + 1}/{max_retries})")
                        continue
                    else:
                        logger.error(f"[API] 无法自动修复 tool call result 顺序问题 - 可能需要查看上面的消息结构")

                # 检测 Missing required arguments 错误（工具参数丢失）
                is_missing_args_error = "Missing required arguments" in error_str or "missing a required argument" in error_str.lower()

                if is_missing_args_error and attempt < max_retries - 1:
                    logger.warning(f"[API] 检测到工具参数丢失错误，尝试从历史消息中恢复...")

                    # 尝试从历史消息中恢复 tool_calls 的参数
                    fixed_messages = self._try_recover_tool_arguments(req_kwargs["messages"])

                    if fixed_messages is not None:
                        fixed_sanitized = messages_to_api(fixed_messages)
                        req_kwargs["messages"] = fixed_sanitized
                        # 更新 API 消息缓存，修复结果持久化，避免下一轮迭代重复修复
                        if use_cache:
                            self._api_messages_cache = fixed_sanitized
                        logger.warning(f"[API] 已恢复工具参数，已更新缓存，重试 (attempt {attempt + 1}/{max_retries})")
                        continue
                    else:
                        logger.warning(f"[API] 无法恢复工具参数，保持现有消息")

                # 其他 BadRequestError 继续抛出
                if hasattr(e, "response") and e.response is not None:
                    resp_body = getattr(e.response, "text", "") or ""
                    logger.error(f"[API] Error response body: {resp_body[:500]}")
                raise
            except Exception as e:
                error_str = str(e)
                error_type = type(e).__name__

                # 判断是否应该重试 - 使用异常继承关系系统性覆盖
                # httpx/httpcore 的异常体系：
                # - NetworkError: 连接失败、协议错误等
                # - TimeoutException: 所有超时（Read/Write/Connect）
                # - ProtocolError: 协议层错误（RemoteProtocolError, LocalProtocolError）
                is_retryable_network = isinstance(e, (httpx.NetworkError, httpcore.NetworkError))
                is_retryable_timeout = isinstance(e, (httpx.TimeoutException, httpcore.TimeoutException))
                is_retryable_protocol = isinstance(e, (httpx.ProtocolError, httpcore.ProtocolError))
                is_rate_limit = isinstance(e, RateLimitError)
                is_server_overload = isinstance(e, APIError) and (
                        "2064" in error_str or "overload" in error_str.lower())
                is_conn_error = isinstance(e, APIConnectionError)
                # 通用 5xx：服务端临时故障（如 MiniMax 的 999/1000、OpenAI 500）应重试
                is_internal_server_error = isinstance(e, InternalServerError)

                should_retry = (
                        is_rate_limit or is_server_overload or is_conn_error or
                        is_retryable_network or is_retryable_timeout or is_retryable_protocol or
                        is_internal_server_error
                )

                if should_retry and attempt < max_retries - 1:
                    # 🛡️ 已取消则不再 emit 信号也不重试
                    if self._is_cancelled:
                        logger.info("[API] 检测到取消，放弃重试")
                        return None, None

                    wait_time = retry_delay * (attempt + 1)
                    if is_rate_limit:
                        retry_reason = "RateLimit"
                    elif is_server_overload:
                        retry_reason = "ServerOverload"
                    elif is_internal_server_error:
                        retry_reason = "InternalServerError"
                    elif is_retryable_timeout:
                        retry_reason = "Timeout"
                    elif is_retryable_protocol:
                        retry_reason = "ProtocolError"
                    else:
                        retry_reason = "ConnectionError"
                    logger.warning(
                        f"[API] {retry_reason} ({error_type}): {error_str[:120]}, "
                        f"retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})"
                    )
                    # 通知 UI 重试状态
                    self.retry_status.emit(retry_reason, attempt + 1, max_retries, wait_time)
                    # 可取消的睡眠：每0.5秒检查一次取消标志
                    elapsed = 0.0
                    step = 0.5
                    while elapsed < wait_time:
                        if self._is_cancelled:
                            logger.info("[API] 重试等待被用户取消")
                            return None, None
                        time.sleep(min(step, wait_time - elapsed))
                        elapsed += step
                    continue

                if hasattr(e, "response") and e.response is not None:
                    resp_body = getattr(e.response, "text", "") or ""
                    logger.error(f"[API] Error response body: {resp_body[:500]}")
                raise

        # 用户在重试等待中取消时 response 可能为 None
        if response is None:
            return False, False
        try:
            return self._process_response(response)
        except (httpx.ReadError, httpcore.ReadError):
            # 🛡️ 连接关闭异常（由 cancel() 关闭 HTTP 连接导致），视为用户取消
            # 此时 _response_chunks 和 _response_content_blocks 已有全部已接收数据，
            # 后续由 run() 中的 if self._is_cancelled: 分支保存 partial 响应
            return False, False

    def _cap_max_output_tokens(self, model: str, requested: int) -> int:
        """
        计算 max_tokens 的合理上限。

        核心原则：
        - 用户明确设置的 max_tokens 应被尊重，provider 默认值不再作为硬上限
        - 仅对已知的模型特定限制做软提示（不强制截断）
        - 对于 write/edit 等工具调用场景，需要足够的输出 token
          来生成完整的 arguments JSON（含长 content）

        Args:
            model: 模型名称
            requested: 用户配置中请求的 max_tokens

        Returns:
            合理的 max_tokens 值
        """
        try:
            requested_int = int(requested)
        except Exception:
            return requested

        profile = get_provider_profile(self.llm_config)

        # 1. 如果用户没有设置或设置值 <= 0，使用 provider 默认值
        if requested_int <= 0:
            return int(profile.get("max_output_tokens", 8192))

        # 2. 获取绝对上限（防止用户设置极端值）
        absolute_limit = int(profile.get("absolute_limit", 65536))

        # 3. 针对特定模型系列的软限制（仅当用户设置值超出时才生效）
        family = profile.get("family", "")
        model_name = (model or "").lower()

        if family == "openai":
            if "gpt-4-turbo" in model_name:
                # GPT-4-Turbo 实际限制 4096，超出会报错
                if requested_int > 4096:
                    return 4096
            # o1/o3 系列支持高输出
            # 其他 openai 模型一般 16384，但用户明确设更高就尊重
        elif family == "anthropic":
            # Claude 系列上限一般为 8192
            pass
        elif family == "minimax":
            pass  # MiniMax 支持高输出

        # 4. 只做绝对上限保护（避免明显错误的极值）
        return min(requested_int, absolute_limit)

    def _process_response(self, response):
        # 🛡️ 保存响应引用，供 cancel() 关闭底层 HTTP 连接以中断流式等待
        self._current_response = response
        self._response_content_blocks = []
        self._current_tool_calls = {}  # 改成字典，key 是 tool_call_id
        self._tool_calls_buffer = {}
        tool_calls_found = False
        tool_args_pending = True
        reasoning_started_this_call = False  # 本轮 API 调用是否已发射 thinking_started
        _reasoning_batch = ""  # 批量积累 reasoning，减少信号频率
        _reasoning_batch_time = time.time()  # 上次发射时间
        _content_batch = ""  # 批量积累 content，减少信号频率
        _content_batch_time = time.time()  # 上次发射 content 的时间
        chunk_count = 0  # chunk 计数器，用于定期 yield 主线程
        self._mem_total_chunks_logged = 0  # 累计流式 chunk 计数
        # 流式开始时记录 RSS 基线（用于自适应 GC）
        self._streaming_rss_base = 0.0
        if _HAS_PSUTIL:
            try:
                self._streaming_rss_base = _psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
            except Exception:
                pass
        for chunk in response:
            if self._is_cancelled:
                # 🛡️ 取消前刷新待处理的 content/reasoning 批次，避免丢失最后一批内容
                if _reasoning_batch:
                    self._emit_with_callback("reasoning_content_received", self.reasoning_content_received, _reasoning_batch)
                    _reasoning_batch = ""
                if _content_batch:
                    self._emit_with_callback("content_received", self.content_received, _content_batch)
                    _content_batch = ""
                return False, False  # 返回元组而不是单个布尔值

            # 兼容新模型（如 GPT-5.5）：流式响应可能包含 choices 为空的 chunk
            # （例如 usage 事件、ping 事件等），直接跳过即可
            if not chunk.choices:
                #但仍需检查 usage 信息（部分模型在空 choices 的 chunk 中携带 usage）
                usage = getattr(chunk, "usage", None)
                if usage:
                    self._last_usage = {
                        "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                        "completion_tokens": getattr(usage, "completion_tokens", 0),
                        "total_tokens": getattr(usage, "total_tokens", 0),
                    }
                    # 同步更新缓存追踪器
                    self._cache_tracker.record_usage(usage)
                continue

            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)

            tool_calls = getattr(delta, "tool_calls", None)
            if tool_calls:
                tool_calls_found = True

                # 🔧 检测到 tool_calls 时，强制冲刷已积累的内容批处理缓冲
                # 避免：文本因不满 15 字符/50ms 阈值而滞留到流式结束才 emit，
                # 然后流结束立即进入工具执行，导致内容 signal 排队在工具 signal 之后，
                # 用户感知为"文本要等工具执行完才出现"
                if _content_batch:
                    self._emit_with_callback("content_received", self.content_received, _content_batch)
                    _content_batch = ""
                    _content_batch_time = time.time()

                for tc in tool_calls:
                    tc_id = tc.id
                    if tc_id is None:
                        if self._tool_calls_buffer:
                            tc_id = list(self._tool_calls_buffer.keys())[-1]
                        else:
                            continue

                    if tc_id not in self._tool_calls_buffer:
                        self._tool_calls_buffer[tc_id] = {
                            "id": tc_id,
                            "type": getattr(tc, "type", "function"),
                            "function": {"name": "", "arguments": ""},
                        }
                        self._response_content_blocks.append(
                            {
                                "type": "tool_call_marker",
                                "tool_call_id": tc_id,
                            }
                        )

                    buffer = self._tool_calls_buffer[tc_id]
                    tool_name = ""
                    if tc.function and tc.function.name:
                        buffer["function"]["name"] = tc.function.name
                        tool_name = buffer["function"]["name"]

                        # 收到 tool name 时立即添加到 _current_tool_calls（如果是新工具）
                        if tc_id not in self._current_tool_calls:
                            self._current_tool_calls[tc_id] = {
                                "id": tc_id,
                                "type": getattr(tc, "type", "function"),
                                "function": {
                                    "name": tool_name,
                                    "arguments": "",
                                },
                            }

                        if (
                                tool_name
                                and tool_name not in self._DEFERRED_PREVIEW_TOOLS
                                and tc_id not in self._previewed_tool_call_ids
                        ):
                            self._previewed_tool_call_ids.add(tc_id)
                            # preview 阶段：arguments 可能还没接收完，显示 "加载中..." 而不是空 {}
                            preview_args = {"_status": "loading", "_preview_hint": "参数接收中..."}
                            if self.tool_start_callback:
                                self.tool_start_callback(
                                    tc_id, tool_name, preview_args, "preview"
                                )
                            else:
                                self._emit_with_callback(
                                    "tool_call_started", self.tool_call_started,
                                    tc_id, tool_name, preview_args, "preview"
                                )
                    if tc.function and tc.function.arguments:
                        buffer["function"]["arguments"] += tc.function.arguments

                    # 【优化】不在此处逐 chunk 执行 json.loads()。
                    # 对于 write/edit 等超长 content 参数，arguments 可能分 50-200 个 chunks 到达。
                    # 每次全量 json.loads() 都会失败并产生异常开销。
                    # 改为流结束后在 _process_response 末尾一次性解析。
                    if buffer["function"]["name"] and buffer["function"]["arguments"]:
                        # 仅在累积字符串达到一定长度时才尝试预解析（用于更新预览状态）
                        # 对于超长场景（>1000 字符），跳过所有逐块解析，等流结束再做
                        args_len = len(buffer["function"]["arguments"])
                        if args_len <= 1000:
                            try:
                                parsed_args = json.loads(buffer["function"]["arguments"])
                                tool_args_pending = False
                                # 更新 _current_tool_calls 中对应 id 的 arguments
                                if tc_id in self._current_tool_calls:
                                    self._current_tool_calls[tc_id]["function"]["arguments"] = buffer["function"][
                                        "arguments"]
                                # 标记已完成解析（用于决定是否发送 tool_call_started）
                                self._current_tool_calls[tc_id]["_args_parsed"] = True
                                self._tool_calls_buffer.pop(tc_id, None)
                                # 流式中间状态：推送实际参数到 UI 更新预览
                                # 使用 buffer 中的 name 而非局部 tool_name（后续 chunk 可能不含 name 字段）
                                _buf_name = buffer["function"].get("name", tool_name)
                                self._emit_with_callback(
                                    "tool_args_updated", self.tool_args_updated,
                                    tc_id, _buf_name or "工具", parsed_args
                                )
                            except json.JSONDecodeError:
                                # 短参数的 JSON 解析失败，记录到等待队列
                                # 同时也发射长度进度，避免 UI 一直卡在"正在准备参数..."
                                prev = self._last_progress_len.get(tc_id, 0)
                                if not prev or args_len - prev >= 200:
                                    self._last_progress_len[tc_id] = args_len
                                    tail = buffer["function"]["arguments"][-40:].replace('\n', ' ')
                                    progress_args = {
                                        "_status": "loading",
                                        "_preview_hint": f"接收参数中({args_len}字符):{tail}",
                                    }
                                    _buf_name = buffer["function"].get("name", tool_name)
                                    self._emit_with_callback(
                                        "tool_args_updated", self.tool_args_updated,
                                        tc_id, _buf_name or "工具", progress_args
                                    )
                                if tc_id not in self._waiting_tool_params:
                                    self._waiting_tool_params[tc_id] = {
                                        "buffer": buffer,
                                        "attempt_count": 0,
                                        "first_failure_time": time.time(),
                                    }
                                self._waiting_tool_params[tc_id]["attempt_count"] += 1
                        else:
                            # 参数已超过 1000 字符，跳过逐块 JSON 解析以节省开销
                            # 但仍推送长度进度 + 累积尾部预览，让 UI 显示接收进度
                            prev = self._last_progress_len.get(tc_id, 0)
                            if not prev or args_len - prev >= 500:
                                self._last_progress_len[tc_id] = args_len
                                # 取累积参数末尾 50 字符作为实时预览（最新到达的内容）
                                tail = buffer["function"]["arguments"][-50:].replace('\n', ' ')
                                progress_args = {
                                    "_status": "loading",
                                    "_preview_hint": f"接收参数中({args_len}字符):{tail}",
                                }
                                _buf_name = buffer["function"].get("name", tool_name)
                                self._emit_with_callback(
                                    "tool_args_updated", self.tool_args_updated,
                                    tc_id, _buf_name or "工具", progress_args
                                )
                            # 放入等待队列，等流结束后一次性解析
                            if tc_id not in self._waiting_tool_params:
                                self._waiting_tool_params[tc_id] = {
                                    "buffer": buffer,
                                    "attempt_count": 0,
                                    "first_failure_time": None,  # None 表示流中不计算超时
                                }
                            self._waiting_tool_params[tc_id]["attempt_count"] += 1

            # 提取 reasoning_content (DeepSeek V4 thinking mode)
            reasoning_delta = getattr(delta, "reasoning_content", None)
            if reasoning_delta:
                if not reasoning_started_this_call:
                    reasoning_started_this_call = True
                    self._emit_with_callback("thinking_started", self.thinking_started)
                # 性能优化：使用 list append 代替字符串拼接
                self._reasoning_chunks.append(reasoning_delta)
                # 批量发送：积累到 10 字符或 50ms 才 emit，避免高频信号堵塞 Qt 事件队列
                _reasoning_batch += reasoning_delta
                now = time.time()
                if len(_reasoning_batch) >= 10 or (now - _reasoning_batch_time) > 0.05:
                    self._emit_with_callback("reasoning_content_received", self.reasoning_content_received,
                                             _reasoning_batch)
                    _reasoning_batch = ""
                    _reasoning_batch_time = now

            if content:
                # 性能优化：使用 list append + join 代替字符串拼接
                self._response_chunks.append(content)
                self._response_content_blocks = append_text_block(
                    self._response_content_blocks, content
                )
                # 批量发送：积累到 15 字符或 50ms 才 emit，避免高频信号堵塞 Qt 事件队列
                _content_batch += content
                now = time.time()
                if len(_content_batch) >= 15 or (now - _content_batch_time) > 0.05:
                    self._emit_with_callback("content_received", self.content_received, _content_batch)
                    _content_batch = ""
                    _content_batch_time = now

            # 保存 token usage（如果这个 chunk 包含 usage 信息，OpenAI/Groq 流式最后一个chunk会带）
            usage = getattr(chunk, "usage", None)
            if usage:
                self._last_usage = {
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(usage, "completion_tokens", 0),
                    "total_tokens": getattr(usage, "total_tokens", 0),
                }
                # 同步更新缓存追踪器
                self._cache_tracker.record_usage(usage)
            # 每处理 5 个 chunk 就让渡一次 CPU，确保主线程能及时处理排队的 Qt 信号
            # 避免 content_received 等信号堆积到工具执行完毕后一次性处理
            chunk_count += 1
            # [MEM] 每 100 个 chunk 记录一次流式内存快照
            if chunk_count % 100 == 0 and self._mem_diag_enabled:
                chunks_total = sum(len(c) for c in self._response_chunks)
                self._mem_total_chunks_logged += 1
                rss_str = ""
                if _HAS_PSUTIL:
                    try:
                        rss = _psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
                        rss_str = f"rss={rss:.1f}MB "
                    except Exception:
                        pass
                logger.debug(
                    f"[MEM] streaming chunk#{chunk_count} "
                    f"{rss_str}"
                    f"_response_chunks#{len(self._response_chunks)} "
                    f"~{chunks_total // 1024}KB tool_calls@{len(self._current_tool_calls)}"
                )

                # 自适应 GC：每 100 chunk 收集一次，RSS 增量 > 200MB 时堆压缩
                # 🔧 修复：仅在 MEM_DIAG 启用时才执行 gc.collect()，避免无条件 stop-the-world GC 阻塞 UI
                if chunk_count % 100 == 0 and self._mem_diag_enabled:
                    freed = gc.collect()
                    if freed > 10:
                        logger.debug(f"[MEM] 流式 gc.collect() 释放了 {freed} 个对象")
                    # 在此作用域内获取 RSS，不依赖外部块
                    _gc_rss = 0.0
                    if _HAS_PSUTIL:
                        try:
                            _gc_rss = _psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
                        except Exception:
                            pass
                    if _gc_rss > 0 and self._streaming_rss_base > 0 and (_gc_rss - self._streaming_rss_base) > 200:
                        _delta = _gc_rss - self._streaming_rss_base
                        try:
                            import ctypes
                            gc.collect()  # 双重 gc.collect 触发 pymalloc arena 合并
                            msvcrt = ctypes.CDLL('msvcrt.dll')
                            msvcrt._heapmin()
                            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
                            heap = kernel32.GetProcessHeap()
                            if heap:
                                kernel32.HeapCompact(heap, 0)
                            logger.info(f"[MEM] 流式 RSS 增量 {_delta:.0f}MB>200MB，已强制堆压缩")
                        except Exception as e:
                            logger.debug(f"[MEM] 堆压缩失败: {e}")
            # processEvents() 从 worker 线程调用仅处理 worker 线程自身事件，
            # 不会处理主线程事件队列中的跨线程 Qt 信号，因此对内容渲染无帮助。
            # 核心修复见上方「检测到 tool_calls 时强制冲刷 _content_batch」。
            # if chunk_count % 10 == 0:
            #     QCoreApplication.processEvents()

        # 非流式响应：usage 在 response 对象本身（而非 chunk）
        if not self.stream:
            usage = getattr(response, "usage", None)
            if usage:
                self._last_usage = {
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(usage, "completion_tokens", 0),
                    "total_tokens": getattr(usage, "total_tokens", 0),
                }
                # 同步更新缓存追踪器
                self._cache_tracker.record_usage(usage)
                total = getattr(usage, "total_tokens", 0) or 0
                self._accumulated_tokens += total
                # 实时通知外部（如 AutoLoop）更新 token 计数
                if self._token_update_callback and total > 0:
                    self._token_update_callback(total)
        # 冲刷剩余的 reasoning batch 和 content batch
        if _reasoning_batch:
            self._emit_with_callback("reasoning_content_received", self.reasoning_content_received, _reasoning_batch)
        if _content_batch:
            self._emit_with_callback("content_received", self.content_received, _content_batch)

        # 尝试让主线程处理刚排队的 content_received 信号
        # (注：从 worker 线程调用可能仅处理 worker 自身事件，主线程事件在下一轮 event loop 处理)
        QCoreApplication.processEvents()

        # 处理等待完整参数的 tool_calls（超长 arguments 场景）
        # 在所有 chunk 接收完成后，再次尝试解析仍处于等待状态的 tool_calls
        # 性能优化：使用 update 代替创建临时集合
        all_pending_ids = set(self._tool_calls_buffer.keys())
        all_pending_ids.update(self._waiting_tool_params.keys())

        for tc_id in list(all_pending_ids):
            buffer = self._tool_calls_buffer.get(tc_id)
            waiting_info = self._waiting_tool_params.get(tc_id)

            # 如果 buffer 存在，优先使用 buffer
            if not buffer and waiting_info:
                buffer = waiting_info["buffer"]

            if buffer and buffer["function"]["name"] and buffer["function"]["arguments"]:
                args_str = buffer["function"]["arguments"]

                # 无论 tc_id 是否已存在，都尝试解析 JSON
                # fix: 已存在的 tc_id 也必须尝试解析，否则 tool_args_pending 无法设为 False
                if tc_id in self._current_tool_calls:
                    # 先更新 arguments
                    self._current_tool_calls[tc_id]["function"]["arguments"] = args_str

                try:
                    # 尝试 JSON 解析（参数完整时应当成功）
                    parsed_args = json.loads(args_str)
                    tool_args_pending = False
                    if tc_id not in self._current_tool_calls:
                        self._current_tool_calls[tc_id] = {
                            "id": buffer["id"],
                            "type": buffer.get("type", "function"),
                            "function": {
                                "name": buffer["function"]["name"],
                                "arguments": args_str,
                            },
                            "_args_parsed": True,
                        }
                    else:
                        self._current_tool_calls[tc_id]["_args_parsed"] = True
                    # 从等待队列中移除
                    self._waiting_tool_params.pop(tc_id, None)
                    self._tool_calls_buffer.pop(tc_id, None)
                except json.JSONDecodeError as e:
                    # JSON 仍然解析失败，记录详细错误信息
                    if tc_id in self._tool_calls_buffer:
                        self._tool_calls_buffer.pop(tc_id, None)

                    # 检查是否超过最大重试次数
                    attempt_count = waiting_info.get("attempt_count", 0) if waiting_info else 0
                    first_time = waiting_info.get("first_failure_time", 0) if waiting_info else 0
                    wait_duration = time.time() - first_time if first_time else 0

                    # 超过 60 秒或超过 10 次尝试，放弃解析
                    # fix: 放弃解析时也设置 tool_args_pending = False，避免无限循环
                    if wait_duration > 60 or attempt_count >= self._max_param_retry_count:
                        logger.warning(
                            f"[ToolCall] ⚠️ JSON 解析超时/超限，保留原始 arguments: "
                            f"tool={buffer['function']['name']}, "
                            f"args_len={len(args_str)}, "
                            f"attempt_count={attempt_count}, "
                            f"wait_duration={wait_duration:.1f}s, "
                            f"error={str(e)}, "
                            f"preview='{args_str[:100]}...'"
                        )
                        # 保留原始 arguments 字符串，让后续处理决定如何处理
                        if tc_id not in self._current_tool_calls:
                            self._current_tool_calls[tc_id] = {
                                "id": buffer["id"],
                                "type": buffer.get("type", "function"),
                                "function": {
                                    "name": buffer["function"]["name"],
                                    "arguments": args_str,  # 保留原始字符串
                                },
                            }
                        # fix: 放弃解析时标记参数不再 pending，允许继续执行
                        tool_args_pending = False
                        self._waiting_tool_params.pop(tc_id, None)
                    else:
                        # 还在等待中，保持在等待队列
                        if tc_id not in self._waiting_tool_params:
                            self._waiting_tool_params[tc_id] = {
                                "buffer": buffer,
                                "attempt_count": attempt_count + 1,
                                "first_failure_time": first_time,
                            }

        # fix: 所有待处理项都处理完毕后，如果没有任何剩余等待项，标记 args_pending = False
        if tool_calls_found and not self._tool_calls_buffer and not self._waiting_tool_params:
            tool_args_pending = False
            # 确保所有已识别的 tool call 都有原始 arguments（防止参数被跳过导致为空字符串）
            for tc in self._current_tool_calls.values():
                if not tc["function"]["arguments"] and tc.get("id") in all_pending_ids:
                    tc["function"]["arguments"] = "{}"

        # 🛡️ 清除当前响应引用（已完成，不需要被 cancel 关闭）
        if self._current_response is not None:
            self._current_response = None

        # 🔧 修复：流式结束后立即回收临时对象（ChatCompletionChunk/Choice/Delta 链）
        # httpx+OpenAI 客户端在处理 900+ chunk 时创建大量临时 Python 对象，
        # 这些对象在此处已无引用，但 pymalloc arena 碎片仍然占用 RSS。
        # 主动 gc.collect() + Windows HeapCompact 可降低峰值 RSS。
        # 注意：仅在 MEM_DIAG 启用时才执行 gc.collect()，避免无条件 stop-the-world GC 阻塞 UI
        if self._mem_diag_enabled:
            freed_count = gc.collect()
            if freed_count > 100:
                logger.debug(f"[MEM] 流式结束 gc.collect() 释放了 {freed_count} 个对象")

        return tool_calls_found, tool_args_pending

    # ========== 并行工具执行 ==========
    # 用户交互类工具的串行化锁：确保同一时间只有一个工具等待用户响应（权限审批、提问）
    _permission_serializer = threading.Lock()
    # 取消哨兵：与普通 None 返回值区分
    _TOOL_CANCELLED = object()

    def _execute_all_tools(self):
        """
        调度工具执行：根据调用情况选择串行或并行路径。

        - 如果所有工具都是非交互式的（无 question 工具），⏩ 并行执行
        - 如果包含 question 工具，需要用户交互，回退到串行执行
        """
        if not self._current_tool_calls or not self.tool_executor:
            return []

        # 重置工具执行取消标志，开始新的执行周期
        self._tool_execution_cancelled = False

        tool_calls = list(self._current_tool_calls.values())

        # 快速取消检查
        if self._is_cancelled or self._tool_execution_cancelled:
            for tc in tool_calls:
                self._emit_cancelled_tool_result(tc)
            return []

        # 检查是否有 question 工具（需要用户交互，不能并行）
        for tc in tool_calls:
            if tc["function"]["name"] == "question":
                return self._execute_tools_sequential(tool_calls)

        # ⏩ 并行执行
        return self._execute_tools_parallel(tool_calls)

    def _emit_cancelled_tool_result(self, tc):
        """发射单个工具被取消的结果信号"""
        tool_name = tc["function"]["name"]
        tool_call_id = tc["id"]
        try:
            args = json.loads(tc["function"]["arguments"])
        except Exception:
            args = {}
        self._emit_with_callback(
            "tool_result_received",
            self.tool_result_received,
            tool_call_id, tool_name, args,
            type("ToolResult", (),
                 {"success": False, "content": None, "error": "用户中止"})(),
        )

    def _execute_tools_sequential(self, tool_calls):
        """
        串行执行工具调用（保留原有逻辑，用于包含 question 的场景）

        当工具列表中有 question 工具时使用此路径，
        因为 question 需要阻塞等待用户输入。
        """
        results = []
        for tc in tool_calls:
            if self._is_cancelled or self._tool_execution_cancelled:
                self._emit_cancelled_tool_result(tc)
                return []

            tool_name = tc["function"]["name"]
            arguments = tc["function"]["arguments"]
            tool_call_id = tc["id"]
            raw_args = tc["function"]["arguments"]
            round_id = f"round_{id(tc)}"
            original_args_str = arguments

            # ====== JSON 参数解析 ======
            if isinstance(arguments, str):
                parsed, err_result = self._parse_tool_arguments(arguments, tool_name, raw_args, tool_call_id, round_id)
                if parsed is None:
                    if err_result:
                        results.append(err_result)
                    continue
                arguments = parsed

            # ====== 检查必需参数 ======
            result = self._check_required_args(tool_name, arguments, tool_call_id, round_id, original_args_str)
            if result is not None:
                results.append(result)
                continue

            # ====== 发射 tool_call_started ======
            self._emit_tool_started(tool_call_id, tool_name, arguments, round_id)

            # ====== 处理 question 工具 ======
            if tool_name == "question":
                return self._handle_question_tool(tool_call_id, arguments)

            # ====== 权限检查 ======
            should_continue = self._check_permission(tool_name, arguments, tool_call_id, round_id, results)
            if not should_continue:
                return None  # 取消
            if results and results[-1] and results[-1].get("tool_call_id") == tool_call_id:
                continue  # 权限拒绝，跳过执行

            # ====== 执行工具 ======
            result_obj, result_content, success = self._execute_tool(
                tool_name, arguments, tool_call_id
            )
            if result_obj is self._TOOL_CANCELLED:
                return None

            # ====== 发射结果 ======
            self._emit_with_callback("tool_result_received", self.tool_result_received,
                                     tool_call_id, tool_name, arguments, result_obj)
            results.append(self._build_result_dict(tool_call_id, tool_name, arguments, result_content,
                                                   success, round_id, result_obj))

        return results

    def _execute_tools_parallel(self, tool_calls):
        """
        ⏩ 并行执行所有工具调用（使用线程池）

        分两阶段：
        1. 预处理：JSON 解析 + 参数校验（串行执行，快速失败）
        2. 执行：将有效工具提交到 ThreadPoolExecutor 并行执行
        """
        # ====== Phase 1: 预处理（串行） ======
        tasks = []  # [(index, tool_name, call_id, args, round_id), ...]
        pre_results = []  # 预处理阶段产生的错误结果

        for idx, tc in enumerate(tool_calls):
            if self._is_cancelled or self._tool_execution_cancelled:
                self._emit_cancelled_tool_result(tc)
                return pre_results  # 返回已经收集的错误结果

            tool_name = tc["function"]["name"]
            arguments_str = tc["function"]["arguments"]
            tool_call_id = tc["id"]
            round_id = f"round_{id(tc)}"
            original_args_str = arguments_str

            # JSON 参数解析
            if isinstance(arguments_str, str):
                parsed, err_result = self._parse_tool_arguments(arguments_str, tool_name, arguments_str, tool_call_id, round_id)
                if parsed is None:
                    if err_result:
                        pre_results.append(err_result)
                    continue
                arguments = parsed
            else:
                arguments = arguments_str

            # 检查必需参数
            result = self._check_required_args(tool_name, arguments, tool_call_id, round_id, original_args_str)
            if result is not None:
                pre_results.append(result)
                continue

            tasks.append((idx, tool_name, tool_call_id, arguments, round_id))

        if not tasks:
            return pre_results

        # ====== Phase 2: 并行执行 ======
        parallel_results = []

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(tasks), 8)
        ) as executor:
            future_map = {}
            for idx, tool_name, tool_call_id, arguments, round_id in tasks:
                future = executor.submit(
                    self._execute_one_tool_parallel,
                    tool_name, tool_call_id, arguments, round_id, idx,
                )
                future_map[future] = idx

            # 🛡️ 支持取消：用 wait() 循环替代 as_completed()，每 0.5s 检测取消标志
            # 避免 as_completed() 在工具线程完成前无限阻塞，导致 cancel() 无法生效
            pending = set(future_map.keys())
            while pending:
                if self._is_cancelled or self._tool_execution_cancelled:
                    for f in pending:
                        f.cancel()  # 取消尚未启动的任务
                    break
                done, pending = concurrent.futures.wait(
                    pending, timeout=0.5,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    original_idx = future_map[future]
                    try:
                        result = future.result()
                        if result is not None:
                            parallel_results.append((original_idx, result))
                    except concurrent.futures.CancelledError:
                        pass  # 被取消的任务，忽略
                    except Exception as e:
                        logger.error(f"[ToolCall] 并行工具执行异常: {e}")

        # 按原始索引排序，保持结果顺序稳定
        parallel_results.sort(key=lambda x: x[0])
        all_results = pre_results + [r[1] for r in parallel_results]
        return all_results

    def _parse_tool_arguments(self, arguments_str, tool_name, raw_args, tool_call_id, round_id):
        """
        解析工具参数的 JSON。

        Returns:
            Tuple[dict, dict|None]: (parsed_args, error_result)
            - parsed_args: 解析后的参数字典
            - error_result: 解析失败时的错误结果字典，成功时为 None
        """
        try:
            return json.loads(arguments_str), None
        except json.JSONDecodeError as e:
            # JSON 解析失败，尝试智能修复
            fixed_args = smart_parse_arguments(arguments_str, tool_name)
            if fixed_args is not None:
                logger.info(f"[ToolCall] ✓ JSON 智能修复成功: tool={tool_name}, args={list(fixed_args.keys())}")
                return fixed_args, None
            if not arguments_str or not arguments_str.strip():
                return {}, None
            # 二次修复尝试
            retry_fixed = smart_parse_arguments(arguments_str, tool_name)
            if retry_fixed is not None:
                logger.info(f"[ToolCall] ✓ 二次 JSON 修复成功: tool={tool_name}, args_len={len(arguments_str)}")
                return retry_fixed, None
            # 对于 write/edit 工具尝试原始提取
            if tool_name in ("write", "edit"):
                extracted = _extract_tool_args_from_raw(arguments_str, tool_name)
                if extracted:
                    logger.info(f"[ToolCall] ✓ 原始参数提取成功: tool={tool_name}, keys={list(extracted.keys())}")
                    return extracted, None
            # 彻底失败
            error_result = self._build_json_parse_error(tool_name, raw_args, tool_call_id, round_id, str(e))
            return None, error_result

    def _build_json_parse_error(self, tool_name, raw_args, tool_call_id, round_id, error_msg):
        """构建 JSON 解析失败的错误结果并发射信号"""
        logger.warning(f"[ToolCall] ⚠️ JSON 解析失败且无法修复，tool={tool_name}, error={error_msg}, "
                       f"preview='{raw_args[:200]}...'")
        preview_args = {"_raw_args": raw_args[:500], "_status": "parse_failed"}
        self._emit_tool_started(tool_call_id, tool_name, preview_args, round_id)
        error_result = {
            "success": False, "content": None,
            "error": f"[参数错误] JSON 格式无效: {error_msg}\n"
                     f"工具: {tool_name}\n原始内容(前500字): {raw_args[:500]}...",
        }
        self._emit_with_callback("tool_result_received", self.tool_result_received,
                                 tool_call_id, tool_name, preview_args, error_result)
        return {
            "role": "tool", "tool_call_id": tool_call_id, "name": tool_name,
            "arguments": {"_raw_args": raw_args[:500]},
            "content": error_result["error"], "success": False, "round_id": round_id,
        }

    def _check_required_args(self, tool_name, arguments, tool_call_id, round_id, original_args_str):
        """检查必需参数，缺失则返回错误结果，否则返回 None"""
        required_args = self.tool_executor.REQUIRED_ARGS.get(tool_name, [])
        missing_args = [p for p in required_args if p not in arguments]
        if missing_args:
            logger.warning(f"[ToolCall] ⚠️ 缺少必需参数: tool={tool_name}, missing={missing_args}")
            error_result = {
                "success": False, "content": None,
                "error": f"[参数缺失] 缺少必需参数: {missing_args}\n工具: {tool_name}",
            }
            self._emit_with_callback("tool_result_received", self.tool_result_received,
                                     tool_call_id, tool_name, arguments, error_result)
            return {
                "role": "tool", "tool_call_id": tool_call_id,
                "content": f"Error: Missing required arguments: {missing_args}",
                "round_id": round_id,
            }
        return None

    def _emit_tool_started(self, tool_call_id, tool_name, arguments, round_id):
        """发射 tool_call_started 信号"""
        if self.tool_start_callback:
            self.tool_start_callback(tool_call_id, tool_name, arguments, round_id)
        else:
            self._emit_with_callback("tool_call_started", self.tool_call_started,
                                     tool_call_id, tool_name, arguments, round_id)

    def _handle_question_tool(self, tool_call_id, arguments):
        """处理 question 工具调用（阻塞等待用户回答）"""
        questions = arguments.get("questions", [])
        if not questions and "question" in arguments:
            questions = [{
                "question": arguments["question"],
                "options": arguments.get("options", []),
                "multiple": arguments.get("multiple", False),
            }]
        # 规范化选项格式
        for q in questions:
            opts = q.get("options", [])
            normalized = []
            for opt in opts:
                if isinstance(opt, str):
                    normalized.append({"label": opt, "description": ""})
                elif isinstance(opt, dict):
                    desc = opt.get("description", "")
                    label = opt.get("label")
                    if not label:
                        for key in ("name", "text", "value", "title"):
                            label = opt.get(key)
                            if label:
                                break
                    if not label:
                        if desc and len(opt) <= 1:
                            label = desc
                            desc = ""
                        else:
                            for v in opt.values():
                                if isinstance(v, str):
                                    label = v
                                    break
                            if not label:
                                label = str(opt)
                    normalized.append({"label": label, "description": desc})
                else:
                    normalized.append({"label": str(opt), "description": ""})
            q["options"] = normalized
        extra = {"tool_call_id": tool_call_id}
        self._emit_with_callback("question_asked", self.question_asked, tool_call_id, questions, extra)
        self._question_pending = {"tool_call_id": tool_call_id, "questions": questions}
        return None  # 通知 run() 等待用户回答

    def _check_permission(self, tool_name, arguments, tool_call_id, round_id, results):
        """
        权限检查。返回 False 表示取消/已处理（调用方应 return None），
        True 表示继续执行。
        当权限被拒绝时，自动追加错误结果到 results。
        """
        if not self.permission_check_callback:
            return True

        # 检查权限缓存
        if self._permission_cache.is_allowed(tool_name):
            logger.info(f"[Permission] 使用缓存: tool={tool_name}")
            return True

        permission_result = self.permission_check_callback(tool_name, arguments)

        if permission_result == "ask":
            # 串行化用户交互：同一时间只有一个工具需要用户确认
            with self._permission_serializer:
                # 再次检查缓存（可能已被其他线程处理）
                if self._permission_cache.is_allowed(tool_name):
                    return True
                self._emit_with_callback("permission_approval_requested",
                                         self.permission_approval_requested,
                                         tool_call_id, tool_name, arguments)
                self._permission_pending = {
                    "tool_call_id": tool_call_id, "tool_name": tool_name, "arguments": arguments,
                }
                self._permission_approved = False
            # 锁在 while 循环前释放，避免阻塞主线程调用 approve_permission/deny_permission
            # 性能优化：移除后台线程中的 processEvents()
            # processEvents() 在非主线程调用会导致信号丢失、死锁等问题
            # UI 响应应通过信号-槽机制自然处理
            while (self._permission_pending is not None
                   and not self._is_cancelled
                   and not self._tool_execution_cancelled):
                # if not self._legacy_direct_callbacks:
                #     QApplication.processEvents()  # 移除：后台线程不应调用 processEvents()
                time.sleep(0.1)  # 保留 sleep 用于轮询等待用户授权

            if self._is_cancelled or self._tool_execution_cancelled:
                self._emit_cancelled_tool_result({"id": tool_call_id, "function": {"name": tool_name, "arguments": "{}"}})
                return False

            if not self._permission_approved:
                self._emit_with_callback("tool_result_received", self.tool_result_received,
                                         tool_call_id, tool_name, arguments,
                                         type("ToolResult", (),
                                              {"success": False, "error": "Permission denied by user"})())
                results.append({
                    "role": "tool", "tool_call_id": tool_call_id,
                    "content": "Error: Permission denied by user", "round_id": round_id,
                })
                return True  # 已处理，继续（但不会执行工具）

        return True  # 允许执行

    def _execute_tool(self, tool_name, arguments, tool_call_id):
        """执行单个工具调用。"""
        try:
            result = self.tool_executor.execute(
                tool_name, arguments, call_id=tool_call_id
            )
        except Exception as e:
            logger.error(f"[Tool] Tool '{tool_name}' execution failed: {e}")
            return None, f"Tool execution error: {str(e)}", False

        if self._is_cancelled or self._tool_execution_cancelled:
            self._emit_with_callback("tool_result_received", self.tool_result_received,
                                     tool_call_id, tool_name, arguments,
                                     {"success": False, "content": None, "error": "用户中止"})
            return self._TOOL_CANCELLED, None, None

        result_content = str(result) if result else ""
        success = bool(getattr(result, "success", True)) if result else False
        return result, result_content, success

    def _build_result_dict(self, tool_call_id, tool_name, arguments, result_content, success, round_id, result_obj):
        """构建标准的结果字典"""
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "arguments": arguments or {},
            "content": result_content,
            "success": success,
            "round_id": round_id,
            "diff": getattr(result_obj, "diff", None) if result_obj else None,
            "anchors": getattr(result_obj, "anchors", None) if result_obj else None,
            "echarts": getattr(result_obj, "echarts", None) if result_obj else None,
        }

    def _execute_one_tool_parallel(self, tool_name, tool_call_id, arguments, round_id, idx):
        """
        在 ThreadPoolExecutor 线程中执行单个工具调用（并行路径）。

        Args:
            tool_name: 工具名称
            tool_call_id: 工具调用 ID
            arguments: 已解析的参数 dict
            round_id: 轮次 ID
            idx: 原始索引（用于排序）

        Returns:
            dict: 工具结果，或 None（取消）
        """
        try:
            # ====== 发射 tool_call_started（非关键，失败不阻断） ======
            try:
                self._emit_tool_started(tool_call_id, tool_name, arguments, round_id)
            except Exception as e:
                logger.warning(f"[ToolCall] 发射 tool_call_started 失败: {e}")

            # ====== 权限检查 ======
            results_placeholder = []
            try:
                should_continue = self._check_permission(
                    tool_name, arguments, tool_call_id, round_id, results_placeholder
                )
            except Exception as e:
                logger.warning(f"[ToolCall] 权限检查失败: {e}")
                should_continue = True  # 权限检查失败时默认放行

            if not should_continue:
                return None  # 取消
            if results_placeholder:
                return results_placeholder[0]  # 权限拒绝的结果

            # ====== 执行工具 ======
            result_obj, result_content, success = self._execute_tool(
                tool_name, arguments, tool_call_id
            )
            if result_obj is self._TOOL_CANCELLED:
                return None

            # ====== 发射结果信号（非关键，失败不阻断） ======
            try:
                self._emit_with_callback("tool_result_received", self.tool_result_received,
                                         tool_call_id, tool_name, arguments, result_obj)
            except Exception as e:
                logger.warning(f"[ToolCall] 发射 tool_result_received 失败: {e}")

            return self._build_result_dict(
                tool_call_id, tool_name, arguments, result_content, success, round_id, result_obj
            )

        except Exception as e:
            logger.error(f"[ToolCall] 并行工具执行异常: tool={tool_name}, error={e}")
            return self._build_result_dict(
                tool_call_id, tool_name, arguments,
                f"Tool execution error: {str(e)}", False, round_id, None
            )

    def _handle_error(self, error):
        from openai import (
            BadRequestError,
            RateLimitError,
            APIConnectionError,
            APITimeoutError,
            APIError,
        )

        error_msg = str(error)

        if (
                "peer closed connection" in error_msg.lower()
                or "incomplete chunked read" in error_msg.lower()
        ):
            self._emit_with_callback(
                "error_occurred", self.error_occurred,
                f"[连接中断] 服务器在响应中途关闭了连接，可能是服务器过载或网络不稳定。请稍后重试。"
            )
            return
        if "ProtocolError" in error_msg or "RemoteProtocolError" in error_msg:
            self._emit_with_callback(
                "error_occurred", self.error_occurred,
                f"[连接错误] 网络协议错误，可能是服务器关闭了连接。请稍后重试。"
            )
            return

        if isinstance(error, BadRequestError):
            # 检测参数过大相关错误（不同 provider 的不同错误信息）
            if any(kw in error_msg.lower() for kw in
                   ["too large", "too long", "exceeds", "maximum length",
                    "413", "payload", "request entity"]):
                self._emit_with_callback(
                    "error_occurred", self.error_occurred,
                    f"[参数过长] 请求参数超出 provider 限制。\n"
                    f"这可能是工具调用的参数（如 write 的 content）过长导致的。\n"
                    f"建议: 减少一次性写入的内容长度，分批写入。\n"
                    f"详情: {error_msg[:300]}"
                )
            elif "json" in error_msg.lower() or "format" in error_msg.lower():
                self._emit_with_callback(
                    "error_occurred", self.error_occurred,
                    f"[JSON格式错误] 请确保输入有效的JSON格式: {error_msg}"
                )
            elif "tool" in error_msg.lower() and any(kw in error_msg.lower()
                                                      for kw in ["argument", "parameter", "missing", "required"]):
                self._emit_with_callback(
                    "error_occurred", self.error_occurred,
                    f"[工具参数错误] 模型生成的工具参数不完整或格式错误。\n"
                    f"建议: 重新发送请求重试。\n"
                    f"详情: {error_msg[:300]}"
                )
            else:
                self._emit_with_callback("error_occurred", self.error_occurred, f"[请求错误] {error_msg}")
        elif isinstance(error, RateLimitError):
            self._emit_with_callback(
                "error_occurred", self.error_occurred,
                f"[速率限制] 请求过于频繁，请稍后再试。详情: {error_msg}"
            )
        elif isinstance(error, APIConnectionError):
            self._emit_with_callback(
                "error_occurred", self.error_occurred,
                f"[连接失败] 无法连接到 API 服务器，请检查网络或 API_URL 设置。详情: {error_msg}"
            )
        elif isinstance(error, APITimeoutError):
            self._emit_with_callback(
                "error_occurred", self.error_occurred,
                f"[超时] 请求超时（300秒），请检查网络或模型负载。详情: {error_msg}"
            )
        elif isinstance(error, APIError):
            if "context length" in error_msg and "overflow" in error_msg:
                self._emit_with_callback(
                    "error_occurred", self.error_occurred,
                    f"[上下文超限] 输入内容过长，请缩短对话或清除历史记录。详情: {error_msg}"
                )
            elif "insufficient_quota" in error_msg:
                self._emit_with_callback(
                    "error_occurred", self.error_occurred,
                    f"[配额不足] API配额已用完，请检查账户余额或更换API Key。"
                )
            else:
                self._emit_with_callback("error_occurred", self.error_occurred, f"[API错误] {error_msg}")
        elif "unrecognized_parameter" in error_msg or "extra_parameters" in error_msg:
            self._emit_with_callback(
                "error_occurred", self.error_occurred,
                f"[兼容性提示] 当前模型可能不支持某些高级设置（如思考模式或温度）。错误: {error_msg}"
            )
        elif "max_tokens" in error_msg.lower() or "context length" in error_msg.lower():
            self._emit_with_callback(
                "error_occurred", self.error_occurred,
                f"[错误] 模型上下文或最大Token超出限制，请减少输入长度或调低 max_tokens"
            )
        elif "authentication" in error_msg.lower() or "api key" in error_msg.lower():
            self._emit_with_callback("error_occurred", self.error_occurred,
                                     f"[认证错误] API Key无效或已过期，请检查配置。")
        else:
            self._emit_with_callback("error_occurred", self.error_occurred, f"[未知错误] {error_msg}")


def _extract_tool_args_from_raw(raw_str: str, tool_name: str) -> dict:
    """
    从无法解析的原始 JSON 字符串中，针对文件操作工具提取关键参数。

    适用于 LLM 生成的 write/edit/patch 等工具的 arguments 字符串，
    当标准 JSON 解析和 smart_parse_arguments 都失败时使用。
    通过精确的字符串搜索提取 path、content、oldString、newString 等字段。

    Args:
        raw_str: 原始 arguments 字符串
        tool_name: 工具名称

    Returns:
        提取的参数字典，提取失败返回空 dict
    """
    import re as _re

    result = {}

    # 提取 path 字段
    path_match = _re.search(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_str)
    if path_match:
        result["path"] = path_match.group(1)

    # 提取 filePath 字段（如果 path 没找到）
    if "path" not in result:
        fp_match = _re.search(r'"filePath"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_str)
        if fp_match:
            result["path"] = fp_match.group(1)

    # 提取 content 字段（write 工具）
    # 策略：找到 "content": " 然后找到其对应的闭合 "，
    # 对于超长 content，从 content 开始到字符串末尾提取，
    # 然后移除末尾多余的 JSON 后缀（如 ", "path": ...})
    if tool_name == "write":
        content_start = raw_str.find('"content"')
        if content_start >= 0:
            colon = raw_str.find(':', content_start)
            if colon >= 0:
                # 找到 content 值的起始引号
                first_quote = raw_str.find('"', colon)
                if first_quote >= 0:
                    content_begin = first_quote + 1
                    # 尝试找闭合引号（考虑转义）
                    content_end = -1
                    i = content_begin
                    while i < len(raw_str):
                        if raw_str[i] == '\\' and i + 1 < len(raw_str):
                            i += 2  # 跳过转义序列
                        elif raw_str[i] == '"':
                            content_end = i
                            break
                        else:
                            i += 1
                    if content_end > content_begin:
                        result["content"] = raw_str[content_begin:content_end]

    # 提取 operations 数组（hashline edit 工具）
    if tool_name == "edit":
        ops_start = raw_str.find('"operations"')
        if ops_start >= 0:
            colon = raw_str.find(':', ops_start)
            if colon >= 0:
                arr_start = raw_str.find('[', colon)
                if arr_start >= 0:
                    # 找匹配的 ]（考虑嵌套）
                    depth = 0
                    arr_end = -1
                    for i in range(arr_start, len(raw_str)):
                        if raw_str[i] == '[':
                            depth += 1
                        elif raw_str[i] == ']':
                            depth -= 1
                            if depth == 0:
                                arr_end = i + 1
                                break
                    if arr_end > arr_start:
                        try:
                            import json
                            result["operations"] = json.loads(raw_str[arr_start:arr_end])
                        except json.JSONDecodeError:
                            pass

    return result


def _compact_process_heap():
    """
    在 cleanup() 后调用。强制压缩进程堆，让 Python 分配器
    将空闲内存 arena 归还给操作系统，降低 RSS。

    跨平台：
    - Windows: HeapCompact
    - Linux: malloc_trim(0)
    - macOS: 无直接等价 API，仅 gc.collect()

    安全说明：HeapCompact 在测试环境可能触发 access violation，
    使用 _safe_ctypes_call 包装避免进程崩溃。
    """
    import sys as _sys

    # 检测测试环境：跳过堆压缩避免 access violation
    if _sys.argv[0].endswith('pytest') or 'PYTEST_CURRENT_TEST' in os.environ:
        return

    before = _psutil.Process(os.getpid()).memory_info().rss if _HAS_PSUTIL else 0

    def _call(fn, *args):
        """安全调用 ctypes 函数，捕获 SEH 异常"""
        try:
            return fn(*args)
        except Exception:
            return 0

    # ========== 核心修复：先释放 Python 对象，再压缩底层 C 堆 ==========
    # 如果跳过 gc.collect() 直接调 HeapCompact/malloc_trim，
    # 堆中充满了 Python 残留对象，压缩后能归还 OS 的内存极少。
    # 使用 gc.collect(2) 收集三代（全量）——cleanup 是低频操作，
    # 全量收集对性能影响可忽略，且能释放最多内存。
    gc.collect(2)

    try:
        if _sys.platform == 'win32':
            import ctypes
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            heap = _call(kernel32.GetProcessHeap)
            if not heap:
                return
            _call(kernel32.HeapCompact, heap, 0)
            # freed 返回值不稳定，不 try to log it

        elif _sys.platform == 'linux':
            try:
                import ctypes
                libc = ctypes.CDLL('libc.so.6', use_last_error=True)
                if _call(libc.malloc_trim, 0) != 0:
                    logger.info("[MEM-HEAP] Linux malloc_trim(0) 释放了空闲堆内存")
            except Exception:
                pass

        elif _sys.platform == 'darwin':
            logger.debug("[MEM-HEAP] macOS:  HeapCompact/malloc_trim 不可达，gc.collect(2) 已在上面调用")
    except Exception:
        pass

    if _HAS_PSUTIL and before:
        after = _psutil.Process(os.getpid()).memory_info().rss
        saved = before - after
        if saved > 10 * 1024 * 1024:
            logger.info(f"[MEM-HEAP] 堆压缩后 RSS 下降 {saved / 1024 / 1024:.1f} MB")


def _get_log_dir_path(filename: str) -> str:
    """获取日志目录路径"""
    try:
        from app.utils.utils import get_app_data_dir
        return str(get_app_data_dir() / "logs" / filename)
    except Exception:
        return filename