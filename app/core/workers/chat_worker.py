# -*- coding: utf-8 -*-
"""
Chat Worker - OpenAI 对话执行器
"""

import concurrent.futures
import gc
import os
import queue
import re
import threading
import time

# 可选依赖：psutil 用于内存诊断（不强制）
try:
    import psutil as _psutil

    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False
    _psutil = None
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import httpcore
import httpx
import orjson as json
from loguru import logger
from openai import (
    APIConnectionError,
    APIError,
    BadRequestError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from PyQt5.QtCore import QCoreApplication, QThread, pyqtSignal

from app.constants import PARAM_SCHEMA, QUOTA_EXCLUDE_KEYS

from app.core.conversation.config import PermissionCache
from app.core.message_content import append_text_block, consolidate_messages, messages_to_api, to_api_message
from app.core.model_capabilities import get_model_capabilities
from app.core.provider_profile import detect_provider_family, get_provider_profile
from app.core.tool_call_parser import smart_parse_arguments
from app.core.token_estimator import count_messages_tokens
from app.core.workers.cache_tracker import CacheHitRateTracker
from app.core.workers.chat_worker_state import ChatWorkerState
from app.core.workers.worker_event_bus import WorkerEvent, WorkerEventBus

# ========== 模块级共享线程池 ==========
# 复用而非每次并行工具执行都新建 ThreadPoolExecutor，消除重复创建/销毁开销
# max_workers=8：与原始单次池上限一致，lazy 创建线程（未使用的线程不实际分配）
# thread_name_prefix 便于调试/性能分析时识别池来源
_SHARED_TOOL_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=min(8, os.cpu_count() or 4),
    thread_name_prefix="tool_parallel",
)

# 预编译正则表达式
_VALID_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _check_team_member(backend) -> bool:
    """检查当前窗口是否是团队成员（供 hook context 使用）"""
    try:
        wid = getattr(backend, "_window_id", None)
        if not wid:
            return False
        from app.core.team_manager import TeamManager

        return TeamManager.get_instance().is_team_member(wid)
    except Exception:
        return False


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
    context_updated = pyqtSignal(int, int, bool)  # token_count, limit, from_api，每轮 API 调用后实时更新
    _DEFERRED_PREVIEW_TOOLS = {"question", "task", "todowrite", "todoread"}

    # ========== 客户端主动循环检测（防止触发 Qwen 服务端 Repetitive tool calls 拒绝）==========
    # Qwen/DashScope 服务端会拒绝"连续多轮相同 (name, args) 的工具调用"，返回 400。
    # 同样的消息序列重试仍会被拒，必须客户端主动中断。
    # 阈值设为 3：与 AutoLoopWorker 的"连续失败 3 次"语义对齐；给模型 1-2 轮自我修正机会。
    _TOOL_LOOP_THRESHOLD = 3

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
        permission_cache: PermissionCache = None,
        compactor=None,
        initial_compaction_cache: Dict = None,
        session_id: str = "",
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
        self.session_id = session_id
        if self.session_id:
            logger.debug(f"[ChatWorker] session_id={self.session_id[:12]}...")
        else:
            logger.debug("[ChatWorker] session_id=EMPTY")

        # ========== 使用 ChatWorkerState 统一管理所有可变状态 ==========
        self._state = ChatWorkerState.from_constructor_args(
            messages=messages,
            session_messages=session_messages or [],
            llm_config=llm_config,
            tools=tools,
            permission_cache=permission_cache or PermissionCache(),
            event_bus=WorkerEventBus(),
            tool_executor=tool_executor,
            compactor=compactor,
            initial_compaction_cache=initial_compaction_cache,
        )
        self._sync_state_from_state()  # 同步到旧属性名（向后兼容）
        # 每轮 API 调用的有效输入上下文计数；API 不返回 usage 时供消息卡片回退显示。
        self._last_context_token_count = 0
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
        self._cached_model_name = model_name  # 缓存，避免 _build_response_message_sequence 重复 get
        if model_name:
            self._cache_tracker.set_model(model_name)

        # 缓存模型是否支持视觉，用于过滤 image_url 块
        self._supports_vision = bool(get_model_capabilities(model_name).get("supports_vision"))

        # ========== 性能优化：API 消息缓存 ==========
        # 向后兼容：保留 PyQt Signal，但通过 EventBus 统一发射
        # UI 层连接这些 signal，事件总线负责分发到所有订阅者

        # 直接回调模式（API 层使用，已迁移到事件总线）
        # 保留以兼容旧的直接回调接口
        self._legacy_direct_callbacks: Dict[str, Callable] = {}

        # ========== HTTP 流式响应引用（供 cancel() 关闭连接）==========
        self._current_response: Any = None

        # ========== 内存诊断 ==========
        self._mem_diag_logged = False  # 防止重复日志刷屏
        self._mem_diag_iter_count = 0  # 诊断计数器（工具迭代轮次）
        self._mem_last_rss = 0.0  # 上一步 RSS 基线（MB）
        self._mem_total_chunks_logged = 0  # 累计记录的流式 chunk 数
        # 环境变量控制：MEM_DIAG=1 启用内存诊断（默认关闭）
        self._mem_diag_enabled = os.environ.get("MEM_DIAG") == "1"
        # tracemalloc 深度追踪（MEM_TRACE=1 时启用，用于定位单步大分配）
        self._mem_trace_enabled = os.environ.get("MEM_TRACE") == "1"
        self._mem_trace_snapshot = None

        # ========== Stop hook 强制续命（Claude Code 兼容）==========
        # 状态机：
        #   False = 正常 turn 结束，Stop hook 可 block 强制续命
        #   True  = 上一轮被 Stop hook 强制续命过，当前 turn 结束时的
        #           Stop 触发后应立即放行（不再 block），避免无限循环
        # 详见 docs/stop_hook_block.md 设计说明
        self._stop_hook_active: bool = False
        # Stop hook 续命时保存上轮的完整响应文本，避免续命后第二轮覆盖丢失
        self._prev_stophook_response: Optional[str] = None

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
                    getattr(self, "_current_session_id", None) or getattr(self, "session_id", None) or "default"
                )
                self._result_persister = ToolResultPersister(session_id=str(session_id))
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
                diff = snap.compare_to(self._mem_trace_snapshot, "lineno")
                large = [d for d in diff if d.size_diff > 10 * 1024 * 1024]
                if large:
                    large.sort(key=lambda d: d.size_diff, reverse=True)
                    log_path = _get_log_dir_path("mem_trace.log")
                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write(f"\n{'=' * 70}\n[MEM-TRACE] 分配 >10MB 的热点:\n{'=' * 70}\n")
                        f.write(f"{'增量MB':>8} {'总计MB':>8} {'计数':>5}  位置\n")
                        f.write(f"{'-' * 70}\n")
                        for stat in large[:15]:
                            frame = stat.traceback[0]
                            loc = (
                                frame.filename.split("site-packages")[-1]
                                if "site-packages" in frame.filename
                                else frame.filename.split("python3")[-1]
                                if "python3" in frame.filename
                                else frame.filename
                            )
                            f.write(
                                f"{stat.size_diff / 1024 / 1024:>7.1f}M {stat.size / 1024 / 1024:>7.1f}M {stat.count_diff:>4}  {loc}:{frame.lineno}\n"
                            )
                            for i, fr in enumerate(stat.traceback[:4]):
                                f.write(
                                    f"   {'├─' if i < 3 else '└─'} {fr.filename.split(os.sep)[-1]}:{fr.lineno} {fr.name}\n"
                                )
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
        self._tool_calls_index_to_id = s.tool_call.index_to_id
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
        s.tool_call.index_to_id = self._tool_calls_index_to_id
        s.response.reasoning_content = self._reasoning_content
        s.http.client = self._http_client
        s.tool_call.execution_cancelled = self._tool_execution_cancelled
        s.tool_call.waiting_params = self._waiting_tool_params
        s.session.current_messages = self._current_session_messages
        s.session.last_usage = self._last_usage
        s.session.accumulated_tokens = self._accumulated_tokens
        # 同步 API 缓存，确保 _sync_state_from_state 不会用旧值覆盖
        s.api_cache.cache = self._api_messages_cache
        s.api_cache.built = self._api_messages_built

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
        self._api_messages_cache = messages_to_api(
            self.messages, supports_vision=self._supports_vision, is_gemini=self._is_gemini_model()
        )
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
            self._api_messages_cache = messages_to_api(
                new_messages, supports_vision=self._supports_vision, is_gemini=self._is_gemini_model()
            )
            return

        # 只转换新消息并追加
        for msg in new_messages:
            api_msg = to_api_message(
                msg, supports_vision=self._supports_vision, is_gemini=self._is_gemini_model()
            )
            if api_msg:
                if api_msg.get("role") == "user" and not api_msg.get("content"):
                    continue
                self._api_messages_cache.append(api_msg)

    def _inject_pending_hook_messages(self, session_messages_target: List = None) -> None:
        """消费 backend 队列中的 PostToolUse 等 hook 消息并注入到 API 缓存。

        在每次 _make_api_call 前调用，确保 LLM 能感知 hook 输出。
        队列中只有 tool_executor 触发的事件（PostToolUse/prompt），预对话事件
        已由 engine.py 直接注入 session.messages，不经过队列。
        不做去重——每条 PostToolUse 对应一次独立的工具调用，都应保留。

        Args:
            session_messages_target: 若传入则同时追加到此列表，
                确保 worker 结束时随 current_session_messages 一起持久化。
        """
        try:
            backend = getattr(self.tool_executor, "_backend", None)
            if not backend:
                return
            q = getattr(backend, "_hook_message_queue", None)
            if q is None:
                return

            msgs = []
            while True:
                try:
                    msgs.append(q.get_nowait())
                except queue.Empty:
                    break

            if msgs:
                self._append_to_api_cache(msgs)
                self._current_session_messages.extend(msgs)
                if session_messages_target is not None:
                    session_messages_target.extend(msgs)
                logger.debug(f"[HookManager] Injected {len(msgs)} hook msgs from queue")
        except Exception as e:
            logger.debug(f"[HookManager] Failed to inject pending hook msgs: {e}")

    def _inject_pending_pretool_messages(self, session_messages_target: List = None) -> None:
        """消费 backend 的 PreToolUse 消息队列，在 tool result 之前注入。

        与 _inject_pending_hook_messages 使用不同的队列，确保 PreToolUse
        在 tool result 之前、PostToolUse 在 tool result 之后出现。
        """
        try:
            backend = getattr(self.tool_executor, "_backend", None)
            if not backend:
                return
            q = getattr(backend, "_pre_tool_message_queue", None)
            if q is None:
                return

            msgs = []
            while True:
                try:
                    msgs.append(q.get_nowait())
                except queue.Empty:
                    break

            if msgs:
                self._append_to_api_cache(msgs)
                self._current_session_messages.extend(msgs)
                if session_messages_target is not None:
                    session_messages_target.extend(msgs)
                logger.debug(f"[HookManager] Injected {len(msgs)} pretool msgs from queue")
        except Exception as e:
            logger.debug(f"[HookManager] Failed to inject pending pretool msgs: {e}")

    def _trigger_worker_hook(
        self,
        event_name: str,
        current_messages: List[Dict],
        current_session_messages: List[Dict],
        extra_context: Dict = None,
    ) -> Optional[str]:
        """在 worker 线程中同步触发 hook 并将输出追加到消息流（只追加不删除）

        Args:
            event_name: hook 事件名（PreAssistantMessage/PostAssistantMessage/Stop 等）
            current_messages: 正在构建的 API 消息列表（in-place 追加）
            current_session_messages: 会话消息列表（in-place 追加）
            extra_context: 注入到 hook context 的额外字段

        Returns:
            block_reason: 如果 hook 决策为 BLOCK，返回提取出的 block 内容
                （来自 hookify 风格 JSON 的 reason/stopReason 字段，或 raw output）；
                否则返回 None。Stop hook 用此实现"强制续命"机制。
        """
        from app.core.backend import _make_hook_message

        try:
            backend = getattr(self.tool_executor, "_backend", None)
            if not backend or not backend.hook_manager:
                return None

            workdir = None
            if backend.tool_executor:
                workdir = backend.tool_executor.get_workdir()
            if not workdir:
                import os as _os

                workdir = _os.getcwd()

            # 获取 session_id
            _session_id = ""
            try:
                _session = backend.get_current_session() if hasattr(backend, "get_current_session") else None
                if _session:
                    _session_id = _session.session_id or ""
            except Exception:
                pass

            ctx = {
                "project_root": workdir,
                "session_id": _session_id,  # Claude Code 兼容字段
                # 【新增】让 hook 能识别当前执行角色（与 subagent_worker._build_hook_context 对齐）
                "current_role": "primary",
                "is_subagent_call": False,
                # 团队上下文：当前窗口是否是团队成员
                "is_team_member": _check_team_member(backend),
            }

            # PreAssistantMessage / PostAssistantMessage：注入上下文使用量信息
            # 让 hook（如 context_auto_compact）能检测当前 token 占比
            if event_name in ("PreAssistantMessage", "PostAssistantMessage"):
                try:
                    from app.core.model_capabilities import resolve_context_limit

                    token_count = count_messages_tokens(current_messages)
                    token_limit = 0
                    llm_config = getattr(self, "llm_config", None)
                    if llm_config:
                        token_limit = resolve_context_limit(llm_config)
                    ctx["token_count"] = token_count
                    ctx["token_limit"] = token_limit
                    if token_count > 0 and token_limit > 0:
                        ctx["token_ratio"] = token_count / token_limit
                    else:
                        ctx["token_ratio"] = 0.0
                except Exception:
                    pass

            if extra_context:
                ctx.update(extra_context)

            # 获取当前用户消息作为 current_message
            current_message_text = ""
            for msg in reversed(current_session_messages):
                if msg.get("role") == "user":
                    from app.core.message_content import content_to_text

                    current_message_text = content_to_text(msg.get("content", ""))
                    break

            # 记录 trigger_event 前的队列大小，用于后续精确 drain
            # 只排出本轮同步执行中入队的消息，不误伤其他路径（如 SubAgentFinished）放入的消息
            _q = getattr(backend, "_hook_message_queue", None)
            qsize_before = _q.qsize() if _q is not None else 0

            results = backend.hook_manager.trigger_event(
                event_name,
                context=ctx,
                current_message=current_message_text,
                trigger_async=False,
            )

            # 🛡️ 精确排出 _hook_message_queue：同步执行路径中 _execute_hook 也会调用
            # on_hook_finished 回调将输出入队，但同步返回值已由下方 results 循环直接
            # 注入消息列表。若不排出，_inject_pending_hook_messages 会在下一轮循环顶部
            # 从队列取出再注入一次，导致重复（尤其是 PROMPT 类型 hook）。
            # ★ 修复：只排出本轮 trigger_event 新增的消息，不误伤其他路径放入的消息
            #   （如 SubAgentFinished，由主线程通过 _inject_subagent_completion_into_stream 放入）
            # 注意：PostToolUse 等事件由 tool_executor 的同步路径触发并通过队列传递，
            # 不经过 _trigger_worker_hook，不受此排出影响。
            if _q is not None:
                qsize_after = _q.qsize()
                to_drain = qsize_after - qsize_before
                for _ in range(to_drain):
                    try:
                        _q.get_nowait()
                    except Exception:
                        break
                if to_drain > 0:
                    logger.debug(
                        f"[HookManager] Drained {to_drain} msg(s) from hook queue"
                        f" after sync trigger_event({event_name})"
                    )

            # 收集所有 hook 结果中的 block reason（按 hook 顺序，最后一个覆盖前面的）
            block_reason: Optional[str] = None
            for r in results:
                # 只有标记为 add_to_context 的 hook 输出才注入消息列表
                # Stop 事件也使用正常的 hook 消息注入（不再依赖 block 决策来决定续命），
                # 通过检测是否有消息被注入到消息列表来决定是否继续工具迭代。
                if r.success and r.output and r.add_to_context:
                    msg = _make_hook_message(event_name, r.output, r.status_message)
                    current_messages.append(msg)
                    current_session_messages.append(msg)
                    self._current_session_messages.append(msg)
                    # 追加到 API 缓存
                    self._append_to_api_cache([msg])

                    logger.debug(
                        f"[HookManager] Worker hook injected: {event_name}, message: {msg.get('content', '')[:100]}..."
                    )

                # 检查 BLOCK 决策（Claude Code Stop hook 强制续命机制）
                # HookDecision.BLOCK 来自 hook_manager.py，对应：
                #   - command hook exit code 2
                #   - JSON 输出 {"decision": "block", ...}
                # 仅 Stop 事件实际消费该决策；其他事件也透传，由调用方决定
                try:
                    from app.core.hook_manager import HookDecision

                    if r.decision == HookDecision.BLOCK:
                        reason = self._extract_block_reason(r.output)
                        if reason:
                            block_reason = reason
                            logger.info(f"[HookManager] Worker hook BLOCK: {event_name} reason_len={len(reason)}")
                except ImportError:
                    pass

            # 检查 auto-compact 触发信号（context_auto_compact hook 的输出）
            # 将 results 中 JSON 的 auto_compact 信号转发给 backend
            if event_name == "PreAssistantMessage":
                self._check_results_auto_compact(results, backend)

            return block_reason

        except Exception as e:
            logger.error(f"[HookManager] Worker hook exception: {event_name} - {e}")
            return None

    def _check_results_auto_compact(self, results: list, backend) -> None:
        """检查 hook 结果中是否有 auto-compact 触发信号

        从各 hook 返回的 JSON 解析 auto_compact 字段，
        有信号则调用 backend.request_auto_compact。

        Args:
            results: trigger_event 返回的 HookExecutionResult 列表
            backend: ChatBackend 实例
        """
        if not results:
            return
        for r in results:
            if not r.success or not r.output:
                continue
            try:
                data = json.loads(r.output)
                if isinstance(data, dict) and data.get("auto_compact"):
                    ratio = float(data.get("ratio", 0.0))
                    backend.request_auto_compact(ratio)
                    return  # 只触发一次
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

    @staticmethod
    def _extract_block_reason(output: str) -> Optional[str]:
        """从 hook 输出中提取 block reason（用于 Stop hook 强制续命）

        优先级（hookify 实际用法 → Claude Code 官方规范 → 兜底）：
        1. JSON `reason` 字段（hookify 默认）
        2. JSON `stopReason` 字段（Claude Code 官方）
        3. JSON `additionalContext` 字段
        4. raw output 兜底

        Args:
            output: hook 原始输出字符串

        Returns:
            提取出的 block reason，None 表示无可用内容
        """
        if not output or not output.strip():
            return None

        # 尝试解析 JSON
        try:
            data = json.loads(output)
            if isinstance(data, dict):
                # 优先级 1: hookify 风格
                if data.get("reason"):
                    return str(data["reason"])
                # 优先级 2: Claude Code 官方
                if data.get("stopReason"):
                    return str(data["stopReason"])
                # 优先级 3: additionalContext
                if data.get("additionalContext"):
                    return str(data["additionalContext"])
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        # 优先级 4: raw output 兜底
        return output

    def _cancel_with_stop_hook(self, current_messages: List[Dict], current_session_messages: List[Dict]) -> None:
        """取消时保存 partial 响应并触发 Stop hook 后发射 finished 信号

        统一处理 3 处取消路径的重复逻辑：
        保存 partial → 触发 Stop hook → 发射 finished_with_messages

        注意：取消路径**不消费 Stop hook 注入的消息**。理由：
        - 用户主动取消时不应被 hook 强制续命
        - 但仍触发 Stop hook，让 hook 知道 assistant 被取消了
        - 重置 _stop_hook_active 状态，避免影响下一轮对话
        """
        partial_sequence = self._build_response_message_sequence()
        if partial_sequence:
            current_messages.extend(partial_sequence)
            current_session_messages.extend(partial_sequence)
            # 🛡️ 清理 orphaned tool_calls：取消时 partial assistant 消息含 tool_calls
            # 但无对应 tool 结果，在持久化前清理，避免下次 API 调用触发 2013 错误
            # 和重复的自动修复开销
            current_messages, _ = self._fix_tool_result_order(current_messages)
            if current_session_messages is not current_messages:
                current_session_messages, _ = self._fix_tool_result_order(current_session_messages)
            self._current_session_messages = list(current_session_messages)
            self.full_response = "".join(self._response_chunks)
            # ====== Stop hook：取消退出前触发 ======
            # 取消时传递 reason="cancelled"，让 hook 能感知取消场景
            self._trigger_worker_hook(
                "Stop",
                current_messages,
                current_session_messages,
                extra_context={
                    "stop_hook_active": self._stop_hook_active,
                    "last_assistant_message": self.full_response,
                    "reason": "cancelled",
                },
            )
            # 取消路径：丢弃 block_reason，不强制续命
            self._stop_hook_active = False
            # 虽然信号可能已被断开，但事件总线仍可能接收
            self._emit_with_callback("finished_with_messages", self.finished_with_messages, current_session_messages)

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

    # 信号名 → WorkerEvent 映射（类变量，避免每次调用重建 dict）
    _SIGNAL_NAME_MAP: dict = {
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

    def _signal_name_to_event(self, signal_name: str) -> Optional[WorkerEvent]:
        """将 signal name 映射到 WorkerEvent（使用类变量缓存）"""
        return self._SIGNAL_NAME_MAP.get(signal_name)

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

    def _restore_partial_content_backup(self):
        """
        恢复协议错误重试时备份的流式内容。

        当 is_retryable_protocol 清空了 _response_content_blocks 但重试全部失败后，
        恢复备份让 run() 的 except 块能构建包含已接收内容的 partial 消息，避免内容丢失。
        """
        backup = getattr(self, "_partial_content_backup", None)
        if not backup:
            return
        # 只有在当前 _response_content_blocks 为空时才恢复（避免覆盖重试成功后的新内容）
        if not self._response_content_blocks:
            self._response_content_blocks = backup.get("content_blocks", []) or []
        if not self._response_chunks:
            self._response_chunks = list(backup.get("response_chunks", []) or [])
        self._partial_content_backup = None

    @staticmethod
    def _detect_repetitive_tool_loop(messages: List[Dict]) -> Optional[Dict]:
        """
        检测消息列表中最近 N 轮的 assistant tool_calls 是否完全一致（即陷入循环）。

        Qwen/DashScope 服务端会拒绝"连续多轮相同 (name, arguments) 的工具调用"。
        同样的请求序列重试仍会被拒，所以必须在客户端主动检测并终止。

        判断标准（与 qwen 服务端语义对齐）：
        - 比较**内容**：`tool_name` + `arguments`（注意：不是 tool_call_id，id 每轮新生成）
        - 比较**轮次**：连续 N 轮 assistant 消息中，如果 tool_calls 签名（按 name+args 排序后
          拼接的 sha256）完全相同 → 触发循环
        - 中间插入任何**不同的** tool_call → 重置计数（不要求与上一轮完全相同才算重复）

        Args:
            messages: 即将发给 API 的完整消息列表（已 consolidate）

        Returns:
            None 表示未检测到循环；
            Dict 表示检测到循环，含 {"rounds": int, "signature": str,
                                       "tool_calls": List[Dict]} 用于构造友好提示。
        """
        threshold = OpenAIChatWorker._TOOL_LOOP_THRESHOLD

        # 从后往前扫，收集最近的 assistant 消息（含 tool_calls 的）
        recent_assistants: List[Dict] = []
        for msg in reversed(messages):
            if msg.get("role") != "assistant":
                continue
            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                # 普通文本消息不算轮次，跳过（继续往前找）
                continue
            recent_assistants.append(msg)
            if len(recent_assistants) >= threshold:
                break

        if len(recent_assistants) < threshold:
            return None

        # 计算每个 assistant 消息的签名：sorted([(name, normalized_args)]) 的 sha256
        signatures = []
        for msg in recent_assistants:
            sig = OpenAIChatWorker._compute_tool_call_signature(msg.get("tool_calls") or [])
            signatures.append(sig)

        # 检查最近 threshold 轮是否完全一致
        if len(set(signatures)) == 1:
            tool_calls = recent_assistants[0].get("tool_calls") or []
            return {
                "rounds": threshold,
                "signature": signatures[0],
                "tool_calls": tool_calls,
            }

        return None

    @staticmethod
    def _compute_tool_call_signature(tool_calls: List[Dict]) -> str:
        """
        计算一轮 tool_calls 的稳定签名。

        排序后再 hash，避免 list 顺序差异导致误判。
        arguments 是字符串，标准化空白后再用：
        - 去除 JSON token（`{}` `,` `:` `"`）周围的装饰性空格
        - 但保留字符串 value 内部的空格（如 "hello world"）
        """
        import hashlib
        import re

        def _normalize_json_whitespace(s: str) -> str:
            # 去掉所有空白（包含换行）后重新插入：
            # 1. JSON token 周围不留空格
            # 2. 字符串 value 内部保留原始字符（只把连续空白压成单空格）
            # 简化版：把所有空白压成单空格，再去掉 `,` `:` `{` `}` `[` `]` 前后的空格
            # 用状态机判断是否在字符串内部太复杂，这里采用保守策略：
            # 先尝试用 json 解析，解析成功则重 dump 规范化；失败则退到"压缩连续空白"。
            try:
                import json

                obj = json.loads(s)
                return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                # 退化：去掉所有空白（包括换行/制表）
                return re.sub(r"\s+", "", s)

        parts = []
        for tc in tool_calls:
            func = tc.get("function") or {}
            name = (func.get("name") or "").strip()
            args = (func.get("arguments") or "").strip()
            args = _normalize_json_whitespace(args)
            parts.append(f"{name}|{args}")
        parts.sort()
        canonical = "\n".join(parts)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _truncate_repetitive_tool_calls(messages: List[Dict], threshold: int) -> List[Dict]:
        """
        从消息列表中移除**所有**连续重复的工具调用轮次，只保留第 1 轮。

        循环检测触发后调用，清理消息历史中的重复工具调用，
        避免下次发消息时再次触发循环检测或被服务端拒绝。

        策略：
        1. 从末尾往前找所有含 tool_calls 的 assistant 消息，计算签名
        2. 找出从末尾开始的**完整连续相同签名区间**（可能超过 threshold 轮）
        3. 保留第 1 轮（assistant + tool 结果）作为正常调用记录
        4. 移除第 2~N 轮的重复 assistant + tool 消息
        5. **保留重复区间之后的所有消息**（如用户刚发的新消息）
        6. 在清理点插入一条 assistant 终止提示（让模型下次换方法）

        Args:
            messages: 会话消息列表（含重复工具调用轮次）
            threshold: 循环检测阈值（即至少重复了多少轮）

        Returns:
            清理后的消息列表（不再包含重复轮次，但保留后续消息）
        """
        # 1. 从后往前收集所有含 tool_calls 的 assistant 消息（index, signature）
        assistants: List[tuple] = []  # [(index, signature), ...] 从后往前
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                sig = OpenAIChatWorker._compute_tool_call_signature(msg.get("tool_calls") or [])
                assistants.append((i, sig))

        if len(assistants) < threshold:
            return messages  # 不足阈值，不需要清理

        # 2. 找出从末尾开始的完整连续相同签名区间
        last_sig = assistants[0][1]
        run_length = 0
        for _idx, sig in assistants:
            if sig == last_sig:
                run_length += 1
            else:
                break

        if run_length < threshold:
            return messages  # 连续相同轮次不足阈值

        # 3. assistants[run_length - 1] 是第 1 个重复轮次（最靠前的那个）
        first_round_assistant_idx = assistants[run_length - 1][0]

        # 4. 找到第 1 轮的 tool 结果结束位置（即需要保留的前缀边界）
        prefix_end = first_round_assistant_idx + 1
        while prefix_end < len(messages) and messages[prefix_end].get("role") == "tool":
            prefix_end += 1

        # 5. 找到最后一个重复轮次的 tool 结果结束位置（即后续消息的起始点）
        last_round_assistant_idx = assistants[0][0]
        tail_start = last_round_assistant_idx + 1
        while tail_start < len(messages) and messages[tail_start].get("role") == "tool":
            tail_start += 1

        # 6. 组装：[前缀含第1轮] + [后续消息（如用户的新消息）]
        # 不插入任何提示消息，让模型自然地从清理后的历史继续
        sanitized = list(messages[:prefix_end])
        sanitized.extend(messages[tail_start:])

        return sanitized

    @staticmethod
    def _detect_vision_tool_loop(messages: List[Dict], threshold: int = 3) -> bool:
        """
        检测最近 N 轮 assistant 是否仅调用了视觉相关工具（screenshot/read）
        且没有任何文本输出，判断模型是否陷入"视觉工具死循环"。

        与 _detect_repetitive_tool_loop 的区别：
        - 后者检测完全相同签名的重复调用（同工具+同参数）
        - 本方法检测仅视觉工具的连续调用（即使轮换不同视觉工具或不同参数）
          如: screenshot → read → screenshot（参数不同，但都是视觉工具）

        判断标准：
        1. 最近 N 轮 assistant 消息全部含 tool_calls
        2. 每轮的 tool_calls 名称全部属于视觉工具集 {"screenshot", "read"}
        3. 都没有文本 content 输出

        Args:
            messages: 消息列表
            threshold: 连续视觉工具轮次阈值（默认 3）

        Returns:
            True 表示检测到了视觉工具死循环
        """
        vision_tools = {"screenshot", "read"}

        # 从后往前扫，收集最近的 assistant 消息
        recent_assistants: List[Dict] = []
        for msg in reversed(messages):
            if msg.get("role") != "assistant":
                continue
            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                # 有文本输出的 assistant 消息 → 视觉循环已中断
                break
            recent_assistants.append(msg)
            if len(recent_assistants) >= threshold:
                break

        if len(recent_assistants) < threshold:
            return False

        # 检查每轮的所有 tool_calls 是否都是视觉工具
        for msg in recent_assistants:
            tool_calls = msg.get("tool_calls") or []
            # 检查是否有文本输出（如果有 content 且非空，说明模型插入了文本，不是纯工具循环）
            content = msg.get("content", "")
            if content and isinstance(content, str) and content.strip():
                return False
            if content and isinstance(content, list):
                # multimodal content blocks
                has_text = any(
                    isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
                    for b in content
                )
                if has_text:
                    return False
            # 检查所有 tool_calls 是否都是视觉工具
            for tc in tool_calls:
                func = tc.get("function") or {}
                name = (func.get("name") or "").strip()
                if name not in vision_tools:
                    return False

        return True

    def _get_reasoning_content(self) -> str:
        """获取当前的 reasoning_content（从累积的 chunks 合成）"""
        if self._reasoning_chunks:
            return "".join(self._reasoning_chunks)
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
        self._current_system_prompt = ""

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
            if hasattr(self, "_event_bus") and self._event_bus is not None:
                self._event_bus.clear()
                self._event_bus = None
        except Exception:
            pass
        if hasattr(self, "_state") and self._state is not None:
            self._state.event_bus = None

        # 🔧 修复：主动压缩进程堆，让 Python 分配器归还空闲 arena 给 OS
        # 防止 RSS 在多次对话间持续增长不下降
        _compact_process_heap()

    # ========== 缓存追踪方法 ==========

    def get_cache_stats(self) -> "AggregatedCacheStats":
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
            for k in (
                "API_KEY",
                "API_URL",
                "模型名称",
                "思考模式",
                "思考等级",
                "思考预算",
                "温度",
                "top_p",
                "最大Token",
                "max_new_tokens",
            )
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
            if cn_key in {
                "API_KEY",
                "API_URL",
                "API_BASE",
                "认证方式",
                "模型名称",
                "系统提示",
                "启用技能",
                "name",
                "provider_name",
                "config_id",
                "display_name",
                "_suffix_index",
                "备注",
                "获取地址",
                "模型列表",
            }:
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
        if self._permission_pending and self._permission_pending.get("tool_call_id") == tool_call_id:
            tool_name = self._permission_pending.get("tool_name", "")
            if auto_allow:
                self._permission_cache.allow_round(tool_name)
            if session_allow:
                self._permission_cache.allow_session(tool_name)
            self._permission_approved = True
            self._permission_pending = None

    def deny_permission(self, tool_call_id: str):
        if self._permission_pending and self._permission_pending.get("tool_call_id") == tool_call_id:
            self._permission_approved = False
            self._permission_pending = None

    def run(self):
        try:
            current_messages = self.messages.copy()
            current_session_messages = list(self.session_messages)
            self._current_session_messages = list(current_session_messages)
            self._emit_compaction_status(self._last_compaction_state)
            self.full_response = ""
            self._prev_stophook_response = None
            self._reasoning_content = ""
            # 开始新对话时，清理所有中间状态
            self._clear_pending_response_state()
            # 🛡️ 防御性清理：移除可能来自上次取消/中断的 orphaned tool_calls
            # （assistant 有 tool_calls 但无对应 tool result），避免 API 2013 错误和重复自动修复开销
            current_messages, _ = self._fix_tool_result_order(current_messages)
            if current_session_messages is not current_messages:
                current_session_messages, _ = self._fix_tool_result_order(current_session_messages)
            self._current_session_messages = list(current_session_messages)

            # 用当前消息初始化 API 缓存（使 _inject_pending_hook_messages 能正确追加）
            self._api_messages_cache = messages_to_api(
                current_messages, supports_vision=self._supports_vision, is_gemini=self._is_gemini_model()
            )
            self._state.api_cache.cache = self._api_messages_cache  # 同步到 state，防止 _sync_state_from_state 覆盖
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
                self._mem_snapshot(
                    "before_api_call",
                    msg_count=len(current_messages),
                    api_cache=len(self._api_messages_cache) if self._api_messages_cache else 0,
                    session_count=len(current_session_messages),
                )

                # ⚠️ 客户端主动循环检测：避免触发 Qwen/DashScope 的 Repetitive tool calls 错误
                # Qwen 服务端会拒绝"连续多轮相同 (name, args) 的工具调用"，返回 400。
                # 同样的请求序列重试仍会被拒，必须在客户端主动中断。
                # 策略：检查 current_messages 中最近 _TOOL_LOOP_THRESHOLD 轮 assistant 消息的
                # tool_call 签名是否完全一致；连续达到阈值就主动终止并提示用户。
                loop_detected = OpenAIChatWorker._detect_repetitive_tool_loop(current_messages)
                if loop_detected:
                    # 静默清理：不报错、不退出，清掉重复轮次后让模型带着干净历史继续
                    logger.warning(
                        f"[ToolLoop] 检测到连续 {self._TOOL_LOOP_THRESHOLD} 轮重复工具调用，静默清理重复轮次后继续。"
                    )
                    current_session_messages = OpenAIChatWorker._truncate_repetitive_tool_calls(
                        current_session_messages, self._TOOL_LOOP_THRESHOLD
                    )
                    current_messages = OpenAIChatWorker._truncate_repetitive_tool_calls(
                        current_messages, self._TOOL_LOOP_THRESHOLD
                    )
                    self._current_session_messages = list(current_session_messages)
                    # 作废 API 缓存，下一轮 _make_api_call 会从清理后的 current_messages 重建
                    self._api_messages_cache = None
                    self._api_messages_built = False
                    continue

                # ⚠️ 视觉工具循环检测：检测最近 N 轮是否只有截图/读取而无任何文本输出
                # 即使轮换调用不同视觉工具（screenshot→read→screenshot），签名不同无法被
                # 上面的通用循环检测捕获，但同样会浪费大量 token。检测到了就强制截断退出。
                if OpenAIChatWorker._detect_vision_tool_loop(current_messages, threshold=3):
                    logger.warning(
                        "[VisionToolLoop] 检测到连续多轮仅视觉工具调用（无文本输出），"
                        "静默清理视觉工具轮次后继续。"
                    )
                    current_session_messages = OpenAIChatWorker._truncate_repetitive_tool_calls(
                        current_session_messages, OpenAIChatWorker._TOOL_LOOP_THRESHOLD
                    )
                    current_messages = OpenAIChatWorker._truncate_repetitive_tool_calls(
                        current_messages, OpenAIChatWorker._TOOL_LOOP_THRESHOLD
                    )
                    self._current_session_messages = list(current_session_messages)
                    self._api_messages_cache = None
                    self._api_messages_built = False
                    continue

                # 消费队列中的 PostToolUse 等 hook 消息（来自 tool_executor）
                self._inject_pending_hook_messages(session_messages_target=current_session_messages)

                # ====== PreAssistantMessage hook：每次 API 调用前触发 ======
                self._trigger_worker_hook(
                    "PreAssistantMessage",
                    current_messages,
                    current_session_messages,
                )

                # 使用 API 消息缓存（首次会重建，后续复用）
                # usage 必须按 API 调用隔离，避免当前轮缺失 usage 时误用上一轮结果。
                self._last_usage = None
                self._state.session.last_usage = None
                self._last_context_token_count = 0
                tool_calls_found, tool_args_pending = self._make_api_call(current_messages, use_cache=True)
                if self._is_cancelled:
                    self._cancel_with_stop_hook(current_messages, current_session_messages)
                    return
                # ====== 实时上下文占用更新（每轮 API 调用后）======
                # 优先用 API 返回的精确 prompt_tokens，没有则估算
                from_api = False
                if self._last_usage and self._last_usage.get("prompt_tokens", 0) > 0:
                    ctx_count = self._last_usage["prompt_tokens"]
                    from_api = True
                else:
                    try:
                        model_name = str(self.llm_config.get("model", "") or "gpt-4")
                        # 传入 tools=self.tools，与上下文圆环快照的口径一致
                        # （快照走 count_messages_tokens(..., tools=available_tools)，会含工具定义 tokens；
                        #  这里漏传 tools 会让卡片底部的 fallback 估值缺掉工具定义，与圆环对不上）
                        ctx_count = count_messages_tokens(current_messages, model=model_name, tools=self.tools)
                    except (ValueError, TypeError, RuntimeError):
                        ctx_count = 0
                self._last_context_token_count = ctx_count
                if ctx_count > 0 and budget > 0:
                    self.context_updated.emit(ctx_count, budget, from_api)
                # ==============================================
                if tool_calls_found and tool_args_pending:
                    # 🛡️ continue 之前检查取消状态，否则 while 循环直接退出绕过保存
                    if self._is_cancelled:
                        self._cancel_with_stop_hook(current_messages, current_session_messages)
                        return
                    continue
                if self._is_cancelled:
                    self._cancel_with_stop_hook(current_messages, current_session_messages)
                    return

                # [MEM] API 返回后
                self._mem_snapshot(
                    "after_api_call",
                    tool_calls_found=tool_calls_found,
                    tool_args_pending=tool_args_pending,
                    resp_chunks=len(self._response_chunks),
                    resp_blocks=len(self._response_content_blocks),
                    msg_count=len(current_messages),
                )

                if not tool_calls_found:
                    response_sequence = self._build_response_message_sequence()
                    current_messages.extend(response_sequence)
                    current_session_messages.extend(response_sequence)
                    self._current_session_messages = list(current_session_messages)
                    # 更新 API 消息缓存：追加响应消息
                    self._append_to_api_cache(response_sequence)
                    # 性能优化：在发送前才合成完整响应字符串
                    self.full_response = "".join(self._response_chunks)
                    # 🔧 Stop hook 续命恢复：prepend 上次保存的响应文本
                    # 当 Stop hook 注入消息导致续命时（_stop_hook_active=True），
                    # _prev_stophook_response 保存了上一轮（续命前）的完整响应文本。
                    # 在此 prepend 到本轮新生成的响应之前，避免内容丢失。
                    if self._stop_hook_active:
                        prev_resp = getattr(self, "_prev_stophook_response", None)
                        if prev_resp:
                            self.full_response = prev_resp + self.full_response
                            self._prev_stophook_response = None

                    # ====== PostAssistantMessage hook：assistant 响应后触发 ======
                    self._trigger_worker_hook(
                        "PostAssistantMessage",
                        current_messages,
                        current_session_messages,
                        extra_context={"assistant_response": self.full_response},
                    )

                    # ====== Stop hook：正常完成退出循环前触发 ======
                    # Stop hook 通过正常的 hook 消息注入（add_to_context=True）来决定
                    # 是否继续工具迭代，不再依赖 block 决策：
                    #   - 第一次 Stop 触发时 _stop_hook_active=False
                    #   - 如果 Stop hook 向消息列表注入了内容，_stop_hook_active 翻转为 True，
                    #     继续一轮让 LLM 看到注入的消息并响应
                    #   - 重跑出来的 Stop 时 _stop_hook_active=True，直接跳过 hook 执行、
                    #     放行退出，避免重复注入消息
                    # 这限制了续命最多 1 次，避免无限循环。
                    if not self._stop_hook_active:
                        # 第一次 Stop：正常触发 hook
                        stop_extra_ctx = {
                            "stop_hook_active": self._stop_hook_active,
                            "last_assistant_message": self.full_response,
                            "reason": "completed",
                        }
                        # 记录 Stop hook 触发前的消息数，用于检测是否有 hook 消息注入
                        before_stop_count = len(current_messages)
                        self._trigger_worker_hook(
                            "Stop",
                            current_messages,
                            current_session_messages,
                            extra_context=stop_extra_ctx,
                        )

                        # 检查 Stop hook 是否有消息注入到消息列表（通过正常的 add_to_context 机制）
                        # 如果有注入且未触发过续命（_stop_hook_active=False），则继续一轮工具迭代
                        stop_injected = len(current_messages) - before_stop_count
                        if stop_injected > 0:
                            # 1. 翻转 _stop_hook_active：下一轮 Stop 时直接跳过 hook 执行
                            self._stop_hook_active = True
                            # 2. 发射 finished_with_messages 让 UI 看到注入的消息（可选）
                            self._emit_with_callback(
                                "finished_with_messages",
                                self.finished_with_messages,
                                current_session_messages,
                            )
                            logger.info(
                                f"[Stop hook] {stop_injected} message(s) injected via hook, force continuation."
                            )
                            # 🔧 修复：保存本轮 full_response，避免 next round 被覆盖
                            # _clear_pending_response_state 会清空 _response_chunks，
                            # 下一轮 API 调用后 full_response 仅保留新文本，上一轮文本丢失。
                            self._prev_stophook_response = self.full_response
                            # 3. 清理 pending state 后回到 while 顶部重跑 API
                            self._clear_pending_response_state()
                            continue  # 跳回 while 顶部，再来一轮

                    # 真正结束：重置状态
                    self._stop_hook_active = False

                    # ★ 退出前最后一次消费 _hook_message_queue，确保 SubAgentFinished
                    # 等 hook 消息不被遗漏（子智能体可能在最后一轮 API 调用期间完成）
                    self._inject_pending_hook_messages(session_messages_target=current_session_messages)

                    self._emit_with_callback(
                        "finished_with_messages", self.finished_with_messages, current_session_messages
                    )
                    # 🔧 修复：先保存 full_response，再清理状态（_clear_pending_response_state
                    # 内部的 _sync_state_from_state 会用 state 中的旧值覆盖 self.full_response）
                    final_response = self.full_response
                    self._clear_pending_response_state()
                    self._emit_with_callback("finished_with_content", self.finished_with_content, final_response)
                    return

                # [MEM] 执行工具前
                tool_count = len(self._current_tool_calls) if self._current_tool_calls else 0
                self._mem_snapshot("before_tool_exec", tool_count=tool_count, msg_count=len(current_messages))

                tool_results = self._execute_all_tools()

                # ★ 消费 PreToolUse 队列（独立于 PostToolUse 队列）
                # PreToolUse 由 tool_executor 同步触发后入 pre_tool 队列，
                # 此处消费确保出现在 tool result 之前，且不会误消费 PostToolUse。
                self._inject_pending_pretool_messages(session_messages_target=current_session_messages)

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
                    self._emit_with_callback(
                        "tool_result_received",
                        self.tool_result_received,
                        q["tool_call_id"],
                        "question",
                        question_args,
                        result_obj,
                    )
                    response_sequence = self._build_response_message_sequence([question_result])
                    current_messages.extend(response_sequence)
                    current_session_messages.extend(response_sequence)
                    self._current_session_messages = list(current_session_messages)
                    # 更新 API 消息缓存
                    self._append_to_api_cache(response_sequence)
                    self._emit_with_callback(
                        "finished_with_messages", self.finished_with_messages, current_session_messages
                    )
                    self._question_pending = None
                    self._pending_answer = None
                    self._answer_event.clear()
                    continue

                # [MEM] 工具执行完成
                result_count = len(tool_results) if tool_results else 0
                tool_names = [r.get("name", "?") for r in (tool_results or [])[:3]]
                tool_result_sizes = sum(len(str(r.get("content", ""))) for r in (tool_results or []))
                self._mem_snapshot(
                    "after_tool_exec",
                    result_count=result_count,
                    tool_names=",".join(tool_names),
                    result_sizes=f"{tool_result_sizes // 1024}KB"
                    if tool_result_sizes > 1024
                    else f"{tool_result_sizes}B",
                    msg_count=len(current_messages),
                )

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
                # 🔧 修复：消息序列构建完成后立即释放 _response_chunks
                # 流式文本 chunk 已全部合并到 response_sequence 和 full_response 中，
                # _response_chunks deque 不再需要，提前释放避免在整个工具执行期间
                # （_execute_all_tools 可能耗时较长）持有几十 MB 的文本 chunk。
                self._response_chunks.clear()
                current_messages.extend(response_sequence)
                current_session_messages.extend(response_sequence)
                self._current_session_messages = list(current_session_messages)

                # ====== PostAssistantMessage hook：assistant 响应后触发 ======
                # 从 response_sequence 提取完整 assistant 文本作为上下文
                _asst_text = ""
                for _m in response_sequence:
                    if _m.get("role") == "assistant" and _m.get("content"):
                        _c = _m["content"]
                        if isinstance(_c, list):
                            # content blocks: 提取所有 text 块
                            texts = [b.get("text", "") for b in _c if isinstance(b, dict) and b.get("type") == "text"]
                            _asst_text = "\n".join(texts)
                        else:
                            _asst_text = str(_c)
                        break
                self._trigger_worker_hook(
                    "PostAssistantMessage",
                    current_messages,
                    current_session_messages,
                    extra_context={"assistant_response": _asst_text},
                )

                # ========== 视觉模型注入：截图/read图片 → base64 图片 ==========
                vision_injected = self._try_inject_vision_content(
                    tool_results,
                    current_messages,
                    session_messages=current_session_messages,
                )
                # =====================================================

                # [MEM] 消息构建后
                self._mem_snapshot(
                    "after_build_msg",
                    msg_count=len(current_messages),
                    session_count=len(current_session_messages),
                    api_cache=len(self._api_messages_cache) if self._api_messages_cache else 0,
                )

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
                    self._api_messages_cache = messages_to_api(
                        current_messages, supports_vision=self._supports_vision, is_gemini=self._is_gemini_model()
                    )
                    # 它的增长会在 worker 结束时由 _on_messages_updated 的
                    # preserve_compaction=False 清空缓存，下轮发送时由 ContextBudgetAllocator 统一压缩。
                else:
                    # 更新 API 消息缓存
                    # 注意：如果 _try_inject_vision_content 已重建完整缓存，跳过追加避免重复
                    if not vision_injected:
                        self._append_to_api_cache(response_sequence)
                self._emit_with_callback(
                    "finished_with_messages", self.finished_with_messages, current_session_messages
                )

                # ========== 迭代结束：内存快照 ==========
                self._mem_diag_iter_count += 1
                _was_compacted = (  # 检测本轮是否执行了压缩
                    "compacted" in locals() and locals().get("compacted") is not None
                )
                self._mem_snapshot(
                    "end_iter",
                    msg_count=len(current_messages),
                    session_count=len(current_session_messages),
                    api_cache=len(self._api_messages_cache) if self._api_messages_cache else 0,
                    compacted="yes" if _was_compacted else "no",
                )
                # 每 3 轮触发一次 GC，帮助回收循环引用
                # 🔧 修复：仅在 MEM_DIAG 启用时才执行 gc.collect()，避免 stop-the-world GC 阻塞 UI
                if self._mem_diag_enabled and self._mem_diag_iter_count % 3 == 0:
                    before_gc = len(gc.get_objects())
                    gc.collect()
                    after_gc = len(gc.get_objects())
                    freed = before_gc - after_gc
                    if freed > 1000:
                        logger.debug(f"[MEM] GC后释放 {freed} 个对象")

        except Exception as e:
            logger.exception("请求失败!")
            # 🔧 异常时保存已生成的部分消息到会话
            try:
                # ====== Stop hook：异常退出前触发 ======
                # 异常路径：消费 block_reason 但不强制续命（与 cancel 路径一致）
                # 理由：API 已异常，继续调用大概率再次失败，无意义
                _cur_msgs = current_messages if "current_messages" in locals() else self.messages
                _cur_session = (
                    current_session_messages if "current_session_messages" in locals() else list(self.session_messages)
                )
                self._trigger_worker_hook(
                    "Stop",
                    _cur_msgs,
                    _cur_session,
                    extra_context={
                        "stop_hook_active": self._stop_hook_active,
                        "last_assistant_message": self.full_response,
                        "reason": "error",
                    },
                )
                # 异常路径：重置状态，不强制续命
                self._stop_hook_active = False

                # 🛡️ 兜底恢复协议错误重试的备份内容（如果 _make_api_call 的 raise 前恢复未生效）
                # 确保 _build_response_message_sequence 能使用已接收的流式内容构建 partial 消息
                self._restore_partial_content_backup()

                partial_sequence = self._build_response_message_sequence()
                if partial_sequence:
                    current_session_messages.extend(partial_sequence)
                self._current_session_messages = list(current_session_messages)
                # 只发射 finished_with_messages（保存消息到会话），不发射 finished_with_content
                # （避免 UI 将其视为正常完成）
                self._emit_with_callback(
                    "finished_with_messages", self.finished_with_messages, current_session_messages
                )
            except Exception as save_err:
                logger.warning(f"[ChatWorker] Failed to save partial messages on error: {save_err}")
            self._handle_error(e)
        finally:
            # 工具执行完成后，清理 round 缓存（为下一轮 API 调用做准备）
            self._permission_cache.clear_round()

    def _make_assistant_msg(self, content, model_name, reasoning_content, timestamp):
        """构建 assistant 消息 dict（消除 3 处重复构造）"""
        msg = {"role": "assistant", "timestamp": timestamp}
        if content:
            msg["content"] = content
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content
        if model_name:
            msg["model_name"] = model_name
        return msg

    def _build_response_message_sequence(self, tool_results=None) -> List[Dict]:
        """构建响应消息序列（性能优化版）"""
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 预缓存：避免循环内重复调用
        model_name = self._cached_model_name
        reasoning_content = self._get_reasoning_content()
        has_reasoning = bool(reasoning_content)
        has_model_name = bool(model_name)

        # ---- Phase 1: 构建 tool_call_map（三合一） ----
        tool_call_map = {}
        # 来源1: _current_tool_calls
        for tc in (self._current_tool_calls or {}).values():
            if not isinstance(tc, dict):
                continue
            tc_id = tc.get("id")
            if not tc_id:
                continue
            func = tc.get("function") or {}
            tool_call_map[tc_id] = {
                "id": tc_id,
                "type": tc.get("type", "function"),
                "function": {
                    "name": func.get("name"),
                    "arguments": func.get("arguments", "{}"),
                },
                # 🔧 透传 Gemini thought_signature，供下一轮回传避免 400
                "thought_signature": tc.get("thought_signature"),
            }

        # 来源2: _tool_calls_buffer（补充缺失项）
        for tc_id, buf in (self._tool_calls_buffer or {}).items():
            if tc_id in tool_call_map:
                continue
            func = buf.get("function") or {}
            tool_call_map[tc_id] = {
                "id": tc_id,
                "type": buf.get("type", "function"),
                "function": {
                    "name": func.get("name", ""),
                    "arguments": func.get("arguments", "{}"),
                },
                "thought_signature": buf.get("thought_signature"),
            }

        # 来源3: tool_results 中未记录的回填
        for item in tool_results or []:
            if not isinstance(item, dict):
                continue
            tc_id = item.get("tool_call_id")
            if tc_id and tc_id not in tool_call_map:
                tool_call_map[tc_id] = {
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                }

        # ---- Phase 2: 构建 tool_result_map ----
        tool_result_map = {}
        if tool_results:
            for item in tool_results:
                if not isinstance(item, dict):
                    continue
                tc_id = item.get("tool_call_id")
                if not tc_id:
                    continue
                tool_result_map[tc_id] = {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": item.get("name", "tool"),
                    "arguments": item.get("arguments", {}),
                    "content": item.get("content", ""),
                    "success": item.get("success", True),
                    "round_id": item.get("round_id"),
                    "timestamp": item.get("timestamp", now_ts),
                    "diff": item.get("diff"),
                    "anchors": item.get("anchors"),
                    "echarts": item.get("echarts"),
                    "lsp_diagnostic": item.get("lsp_diagnostic"),
                }

        # ---- Phase 3: 过滤无结果的 tool_call（原地 del，避免重建 dict） ----
        if tool_result_map:
            orphan_ids = [tc_id for tc_id in tool_call_map if tc_id not in tool_result_map]
            for tc_id in orphan_ids:
                logger.warning(f"[ToolCall预防] 过滤了无结果的 tool_call: {tc_id[:20]}...")
                del tool_call_map[tc_id]

        # ---- Phase 4: 构建 sequence + 用 set 跟踪已添加的 tool_result ----
        sequence = []
        added_tool_ids = set()
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

                tc_id = str(block.get("tool_call_id") or "")
                tool_call = tool_call_map.get(tc_id)

                # assistant 消息
                asst_msg = {"role": "assistant", "timestamp": now_ts}
                if pending_text_blocks:
                    asst_msg["content"] = pending_text_blocks[0].get("text")
                if tool_call:
                    asst_msg["tool_calls"] = [tool_call]
                if has_reasoning:
                    asst_msg["reasoning_content"] = reasoning_content
                if has_model_name:
                    asst_msg["model_name"] = model_name

                if asst_msg.get("content") or asst_msg.get("tool_calls"):
                    sequence.append(asst_msg)

                pending_text_blocks = []

                tool_result = tool_result_map.get(tc_id)
                if tool_result:
                    sequence.append(tool_result)
                    added_tool_ids.add(tc_id)

        # ---- Phase 5: Orphan tool_result（用集合查找替代 O(n*m) 循环） ----
        for tc_id, tool_result in tool_result_map.items():
            if tc_id in added_tool_ids:
                continue
            tool_call = tool_call_map.get(tc_id)
            asst_msg = {"role": "assistant", "timestamp": now_ts}
            if has_reasoning:
                asst_msg["reasoning_content"] = reasoning_content
            if has_model_name:
                asst_msg["model_name"] = model_name
            if tool_call:
                asst_msg["tool_calls"] = [tool_call]
            if asst_msg.get("tool_calls"):
                sequence.append(asst_msg)
            sequence.append(tool_result)

        # ---- Phase 6: 尾部文本 / 兜底 ----
        if pending_text_blocks:
            sequence.append(
                self._make_assistant_msg(
                    pending_text_blocks[0].get("text"), model_name, reasoning_content if has_reasoning else None, now_ts
                )
            )
        elif not sequence and self.full_response:
            text = append_text_block([], self.full_response)[0].get("text")
            sequence.append(
                self._make_assistant_msg(text, model_name, reasoning_content if has_reasoning else None, now_ts)
            )
        elif not sequence:
            empty = {"role": "assistant", "content": [], "timestamp": now_ts}
            if has_model_name:
                empty["model_name"] = model_name
            sequence.append(empty)

        # ---- Phase 7: token_usage 注入 ----
        usage = None
        if self._last_usage:
            usage = {
                "input": self._last_usage.get("prompt_tokens", 0),
                "output": self._last_usage.get("completion_tokens", 0),
                "total": self._last_usage.get("total_tokens", 0),
            }
        elif self._last_context_token_count > 0:
            # API 未返回 usage：按用户可见口径，仅回退显示本地估算的输入上下文。
            usage = {
                "input": self._last_context_token_count,
                "output": 0,
                "total": self._last_context_token_count,
                "estimated": True,
            }

        if usage:
            for msg in sequence:
                if msg.get("role") == "assistant":
                    msg["token_usage"] = usage
                    break

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

    # ========== 视觉模型图片注入 ==========

    @staticmethod
    def _inject_images_to_user_message(messages: List[Dict], data_uris: List[str]) -> bool:
        """
        将图片 data_uri 注入到 messages 列表中最后一个 user 消息的 content 中。

        纯文本 content 转为 multimodal list（text + image_url），
        已有 list 则追加 image_url 块。

        Returns:
            True 成功注入，False 未找到 user 消息
        """
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if msg.get("role") == "user":
                content = msg.get("content", "")
                for data_uri in data_uris:
                    if isinstance(content, str):
                        content = [
                            {"type": "text", "text": content},
                            {"type": "image_url", "image_url": {"url": data_uri}},
                        ]
                        msg["content"] = content
                    elif isinstance(content, list):
                        content.append({"type": "image_url", "image_url": {"url": data_uri}})
                return True
        return False

    def _try_inject_vision_content(
        self, tool_results, current_messages, session_messages: Optional[List[Dict]] = None
    ) -> bool:
        """
        截图/read 图片工具结果在视觉模型 → 将图片以 base64 注入到最后一个用户消息。

        触发条件：
        1. 本轮工具执行结果中有成功的 screenshot 工具，或
        2. 有成功的 read 工具且读取的是图片文件（返回了 image_data）
        3. 当前模型支持视觉（supports_vision=True）

        注入方式：
        在最后一条 user 消息的 content 中追加 image_url 块（multimodal list 格式）。
        这样 LLM 在下一轮 API 调用时就能看到图片内容。
        支持同轮注入多张图片（如同时截图 + 读取图片）。

        同时也会注入到 session_messages（如果传入），确保注入的图片在会话历史中持久化，
        跨多轮对话不会丢失。

        Returns:
            bool: True 表示成功注入了图片（调用方应跳过后续的 _append_to_api_cache）
        """
        if not tool_results or not current_messages:
            return False

        # 检查模型是否支持视觉
        model_name = str(self.llm_config.get("模型名称", "") or "")
        caps = get_model_capabilities(model_name)
        if not caps.get("supports_vision"):
            return False

        # ---- 收集所有可注入的图片 data_uri ----
        import base64

        data_uris = []

        for r in tool_results:
            if not isinstance(r, dict) or not r.get("success"):
                continue
            tool_name = r.get("name", "")

            if tool_name == "screenshot":
                # 从 raw_content（原始 ToolResult.content）提取路径
                raw = r.get("raw_content")
                img_path = None
                if isinstance(raw, dict):
                    img_path = raw.get("absolute_path") or raw.get("path")
                if not img_path:
                    content = r.get("content", "")
                    if isinstance(content, dict):
                        img_path = content.get("absolute_path") or content.get("path")
                    elif isinstance(content, str):
                        if content.startswith("{") and "absolute_path" in content:
                            import ast

                            try:
                                d = ast.literal_eval(content)
                                if isinstance(d, dict):
                                    img_path = d.get("absolute_path") or d.get("path")
                            except (ValueError, SyntaxError):
                                pass
                        if not img_path:
                            m = re.search(r"路径[：:]\s*(\S+\.\w+)", content)
                            if m:
                                img_path = m.group(1)
                if img_path and os.path.isfile(img_path):
                    try:
                        with open(img_path, "rb") as f:
                            img_data = base64.b64encode(f.read()).decode("utf-8")
                        ext = os.path.splitext(img_path)[1].lower()
                        mime_map = {
                            ".png": "image/png",
                            ".jpg": "image/jpeg",
                            ".jpeg": "image/jpeg",
                            ".gif": "image/gif",
                            ".webp": "image/webp",
                            ".bmp": "image/bmp",
                        }
                        mime = mime_map.get(ext, "image/png")
                        data_uris.append(f"data:{mime};base64,{img_data}")
                    except Exception as e:
                        logger.warning(f"[Vision] Failed to read screenshot {img_path}: {e}")

            elif tool_name == "read":
                # 从 image_data 字段获取已编码的图片数据
                img_data = r.get("image_data")
                if isinstance(img_data, dict):
                    mime = img_data.get("mime", "image/png")
                    data = img_data.get("data", "")
                    if data:
                        data_uris.append(f"data:{mime};base64,{data}")
                        logger.debug(f"[Vision] read 图片注入: mime={mime}, base64_len={len(data)}")

        if not data_uris:
            return False

        # ---- 注入到 current_messages（用于本轮 API 调用） ----
        injected = self._inject_images_to_user_message(current_messages, data_uris)
        if not injected:
            return False

        # ---- 同步注入到 session_messages（跨轮持久化） ----
        if session_messages is not None:
            self._inject_images_to_user_message(session_messages, data_uris)

        logger.info(f"[Vision] Injected {len(data_uris)} image(s) into user message for {model_name}")

        # ---- 在 tool result 内容中追加视觉提示，防止 LLM 重复截图/读取 ----
        # 从 tool_results 中找出本轮已注入图片的 screenshot / read 工具名，
        # 在对应的 tool result 消息 content 末尾追加一条明确提示，告知 LLM
        # 图片已自动以 base64 形式注入视觉上下文，无需再次截图/读取。
        _tools_injected = set()
        for r in tool_results:
            if isinstance(r, dict) and r.get("success"):
                tn = r.get("name", "")
                if tn in ("screenshot", "read"):
                    _tools_injected.add(tn)
        if _tools_injected:
            _vision_hint = (
                "\n\n[Vision Notice] The screenshot/image has been automatically "
                "injected into your visual context as an image. You can now see "
                "the visual content directly — no need to capture/read again."
            )
            # 在 current_messages 中找到最后一个 role=tool 且 name 匹配的消息
            for msg in reversed(current_messages):
                if msg.get("role") == "tool" and msg.get("name") in _tools_injected:
                    existing = msg.get("content", "")
                    if isinstance(existing, str) and _vision_hint not in existing:
                        msg["content"] = existing + _vision_hint
                    break
            # 同步更新 session_messages 中的同样位置
            if session_messages is not None:
                for msg in reversed(session_messages):
                    if msg.get("role") == "tool" and msg.get("name") in _tools_injected:
                        existing = msg.get("content", "")
                        if isinstance(existing, str) and _vision_hint not in existing:
                            msg["content"] = existing + _vision_hint
                        break

        # 重建 API 缓存：current_messages 已被修改（含 image_url），
        # 但 _api_messages_cache 仍是旧版本（无图片）。
        # 此处立即重建完整缓存，确保后续 append 操作在正确基线上增量更新。
        try:
            self._api_messages_cache = messages_to_api(
                current_messages, supports_vision=self._supports_vision, is_gemini=self._is_gemini_model()
            )
            self._api_messages_built = True
        except Exception as cache_e:
            logger.warning(f"[Vision] Failed to rebuild API cache: {cache_e}")
            self._api_messages_cache = None
            self._api_messages_built = False
        return True  # 重建了完整缓存，调用方应跳过 _append_to_api_cache

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
            has_tool_calls = any(msg.get("role") == "assistant" and msg.get("tool_calls") for msg in messages)
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

    def _is_gemini_model(self) -> bool:
        """当前 worker 是否为 Gemini 模型（需特殊处理 thought_signature）。"""
        try:
            if detect_provider_family(self.llm_config) == "gemini":
                return True
        except Exception:
            pass
        # 兜底：模型名含 gemini（如 models/gemini-3-flash-preview 的 startswith 判断会漏）
        try:
            model = str((self.llm_config or {}).get("模型名称", "") or "").lower()
            if "gemini" in model:
                return True
        except Exception:
            pass
        return False

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

        # 在重试前保存部分接收到的内容备份，用于协议错误重试失败后恢复
        # 协议错误（如 RemoteProtocolError）重试前会清空 _response_chunks 等中间状态，
        # 但如果所有重试都失败，这些内容需要用于异常路径的 _build_response_message_sequence 构建 partial 消息。
        self._partial_content_backup = None

        # 性能优化：使用缓存的 API 消息
        if use_cache and self._api_messages_cache is not None:
            sanitized = self._api_messages_cache
        else:
            sanitized = messages_to_api(
                messages, supports_vision=self._supports_vision, is_gemini=self._is_gemini_model()
            )
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
        # 添加会话标识（帮助服务商区分不同会话的缓存 key / 用量监控）
        if self.session_id:
            req_kwargs["user"] = self.session_id

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
                # 🛡️ 流式响应处理移入重试循环，流式协议错误可完整重试
                try:
                    return self._process_response(response)
                except (httpx.ReadError, httpcore.ReadError):
                    # 用户取消（cancel()关闭HTTP连接），不是真正的错误
                    return False, False
            except BadRequestError as e:
                error_str = str(e)
                # 检测 tool call result 错误码 2013
                is_tool_call_order_error = (
                    "2013" in error_str
                    or "tool call result does not follow tool call" in error_str.lower()
                    or "tool_calls" in error_str.lower()
                )

                if is_tool_call_order_error and attempt < max_retries - 1:
                    # 自动修复 tool result 顺序问题
                    logger.warning("[API] 检测到 tool call result 顺序错误 (2013)，尝试自动修复...")
                    # 🛡️ 仅调用一次修复：req_kwargs["messages"] 与 messages 的 tool_call_id 集合等价
                    # （messages_to_api 是保结构转换），结果直接复用，杜绝重复扫描同一份数据
                    fixed_messages, was_fixed = self._fix_tool_result_order(req_kwargs["messages"])

                    if was_fixed:
                        fixed_sanitized = messages_to_api(
                            fixed_messages, supports_vision=self._supports_vision, is_gemini=self._is_gemini_model()
                        )
                        req_kwargs["messages"] = fixed_sanitized
                        # 更新 API 消息缓存，修复结果持久化，避免下一轮迭代重复修复
                        if use_cache:
                            self._api_messages_cache = fixed_sanitized
                        # 同步修复源头 current_messages：fixed_messages 与 messages 同型（都是内部格式），
                        # 直接 slice 赋值即可固化源头，避免再次调用 _fix_tool_result_order
                        if fixed_messages is not messages:
                            messages[:] = fixed_messages
                        logger.warning(f"[API] 已修复消息顺序，已同步源头，重试 (attempt {attempt + 1}/{max_retries})")
                        continue
                    else:
                        logger.error("[API] 无法自动修复 tool call result 顺序问题 - 可能需要查看上面的消息结构")

                # 检测 Missing required arguments 错误（工具参数丢失）
                is_missing_args_error = (
                    "Missing required arguments" in error_str or "missing a required argument" in error_str.lower()
                )

                if is_missing_args_error and attempt < max_retries - 1:
                    logger.warning("[API] 检测到工具参数丢失错误，尝试从历史消息中恢复...")

                    # 尝试从历史消息中恢复 tool_calls 的参数
                    fixed_messages = self._try_recover_tool_arguments(req_kwargs["messages"])

                    if fixed_messages is not None:
                        fixed_sanitized = messages_to_api(
                            fixed_messages, supports_vision=self._supports_vision, is_gemini=self._is_gemini_model()
                        )
                        req_kwargs["messages"] = fixed_sanitized
                        # 更新 API 消息缓存，修复结果持久化，避免下一轮迭代重复修复
                        if use_cache:
                            self._api_messages_cache = fixed_sanitized
                        logger.warning(f"[API] 已恢复工具参数，已更新缓存，重试 (attempt {attempt + 1}/{max_retries})")
                        continue
                    else:
                        logger.warning("[API] 无法恢复工具参数，保持现有消息")

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
                    "2064" in error_str or "overload" in error_str.lower()
                )
                is_conn_error = isinstance(e, APIConnectionError)
                # 通用 5xx：服务端临时故障（如 MiniMax 的 999/1000、OpenAI 500）应重试
                is_internal_server_error = isinstance(e, InternalServerError)

                should_retry = (
                    is_rate_limit
                    or is_server_overload
                    or is_conn_error
                    or is_retryable_network
                    or is_retryable_timeout
                    or is_retryable_protocol
                    or is_internal_server_error
                )

                if should_retry and attempt < max_retries - 1:
                    # 🛡️ 已取消则不再 emit 信号也不重试
                    if self._is_cancelled:
                        logger.info("[API] 检测到取消，放弃重试")
                        return None, None

                    # 🛡️ 流式协议错误重试：清除已接收的部分响应状态
                    if is_retryable_protocol:
                        # ⚠️ 备份已接收的流式内容，防止重试全部失败后内容丢失
                        # 见 _make_api_call 方法顶部的说明
                        self._partial_content_backup = {
                            "content_blocks": list(getattr(self, "_response_content_blocks", []) or []),
                            "response_chunks": list(getattr(self, "_response_chunks", []) or []),
                        }
                        self._clear_pending_response_state()

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
                            # 🛡️ 恢复备份：协议错误重试清空了 _response_content_blocks，
                            # 取消路径后续会通过 _cancel_with_stop_hook 调用
                            # _build_response_message_sequence 构建 partial 消息，
                            # 必须在此恢复备份让内容能被保存到会话。
                            self._restore_partial_content_backup()
                            return None, None
                        time.sleep(min(step, wait_time - elapsed))
                        elapsed += step
                    continue

                if hasattr(e, "response") and e.response is not None:
                    resp_body = getattr(e.response, "text", "") or ""
                    logger.error(f"[API] Error response body: {resp_body[:500]}")
                # 🛡️ 恢复备份：协议错误重试清空了 _response_content_blocks 但重试失败，
                # 在 raise 前恢复备份，让 run() 的 except 块能通过 _build_response_message_sequence
                # 构建包含已接收内容的 partial 消息，避免内容丢失。
                self._restore_partial_content_backup()
                raise

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

    @staticmethod
    def _extract_thought_signature(tc) -> Optional[str]:
        """从 OpenAI 兼容的 tool_call delta/对象中提取 Gemini thought_signature。

        Gemini OpenAI 兼容端点在 tool_call 上返回 extra_content.google.thought_signature。
        由于 openai SDK 的 BaseModel 配置了 extra='allow'，该字段会被保留在 model_extra，
        可通过 getattr 直接读取。无则返回 None。
        """
        ec = getattr(tc, "extra_content", None)
        if not ec or not isinstance(ec, dict):
            return None
        google = ec.get("google")
        if not isinstance(google, dict):
            return None
        sig = google.get("thought_signature")
        return sig if sig else None

    def _process_response(self, response):
        # 🛡️ 保存响应引用，供 cancel() 关闭底层 HTTP 连接以中断流式等待
        self._current_response = response
        self._response_content_blocks = []
        self._current_tool_calls = {}  # 改成字典，key 是 tool_call_id
        self._tool_calls_buffer = {}
        # Qwen/DashScope 流式 tool_calls：chunk 2+ 会清空 tc.id，用 index→id 映射回真实 id
        self._tool_calls_index_to_id = {}
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
                    self._emit_with_callback(
                        "reasoning_content_received", self.reasoning_content_received, _reasoning_batch
                    )
                    _reasoning_batch = ""
                if _content_batch:
                    self._emit_with_callback("content_received", self.content_received, _content_batch)
                    _content_batch = ""
                return False, False  # 返回元组而不是单个布尔值

            # 兼容新模型（如 GPT-5.5）：流式响应可能包含 choices 为空的 chunk
            # （例如 usage 事件、ping 事件等），直接跳过即可
            if not chunk.choices:
                # 但仍需检查 usage 信息（部分模型在空 choices 的 chunk 中携带 usage）
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
                    # ⚠️ 兼容 Qwen/DashScope 等 OpenAI 兼容协议的流式 tool_calls：
                    # 第一个 chunk 含 id（"call_xxx"）+ name + 空 arguments；
                    # 后续 chunk 仅含 index + arguments（id 清空为 ""，name 清空为 ""）。
                    # 用首 chunk 的真实 tc.id 作为内部统一 key（保证与 _current_tool_calls /
                    # _waiting_tool_params / tool_result.tool_call_id 下游一致），
                    # 借助 _tool_calls_index_to_id 映射在 id 缺失时找回真实 id。
                    tc_index = getattr(tc, "index", None)
                    raw_id = tc.id
                    tc_id = None

                    # 1. 优先用 raw_id 匹配现有 buffer
                    if raw_id and raw_id in self._tool_calls_buffer:
                        tc_id = raw_id
                    # 2. 否则用 index 映射回真实 id（处理 qwen 等 id 缺失场景）
                    elif tc_index is not None and tc_index in self._tool_calls_index_to_id:
                        tc_id = self._tool_calls_index_to_id[tc_index]

                    # ⚠️ 关键修复（2026-06-25 qwen 工具永远卡在"接收参数中"）：
                    # 修复前代码 `elif self._tool_calls_buffer: tc_id = next(reversed(...))`
                    # 会把第二个 tool_call 的内容错合并到第一个 buffer，导致：
                    # 1) 多 tool_call 并行时 name 互相覆盖
                    # 2) Qwen 末尾 `id=""` 的孤立 chunk 被合并进已有 buffer
                    # 新逻辑：找不到匹配 buffer 时，必须含 name 才创建新条目，避免孤立 buffer
                    # 累积导致 tool_args_pending 永远 True、主循环死锁。
                    if not tc_id:
                        # 必须含 name 才允许创建新 buffer（孤立 delta chunk 跳过）
                        if not (tc.function and tc.function.name):
                            continue
                        # 用真实 id 作为 key（缺 id 时退化用 index）
                        tc_id = raw_id if raw_id else (f"index_{tc_index}" if tc_index is not None else None)
                        if not tc_id:
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
                        # 记录 index → id 映射（供后续 chunk 查找）
                        if tc_index is not None:
                            self._tool_calls_index_to_id[tc_index] = tc_id

                    buffer = self._tool_calls_buffer[tc_id]
                    # 🔧 Gemini thought_signature：必须随 tool call 透传，否则多轮工具调用会 400。
                    # 签名可能出现在任意 delta（通常与 id+name 同片，也可能在 arguments 之后的独立片），
                    # 这里每次 delta 都尝试提取并落到 buffer / _current_tool_calls。
                    sig = self._extract_thought_signature(tc)
                    if sig:
                        buffer["thought_signature"] = sig
                        if tc_id in self._current_tool_calls:
                            self._current_tool_calls[tc_id]["thought_signature"] = sig
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
                                "thought_signature": buffer.get("thought_signature"),
                            }

                        if (
                            tool_name
                            and tool_name not in self._DEFERRED_PREVIEW_TOOLS
                            and tc_id not in self._previewed_tool_call_ids
                        ):
                            self._previewed_tool_call_ids.add(tc_id)
                            # preview 阶段：arguments 可能还没接收完，显示 "加载中..." 而不是空 {}
                            preview_args = {"_status": "loading"}
                            if self.tool_start_callback:
                                self.tool_start_callback(tc_id, tool_name, preview_args, "preview")
                            else:
                                self._emit_with_callback(
                                    "tool_call_started",
                                    self.tool_call_started,
                                    tc_id,
                                    tool_name,
                                    preview_args,
                                    "preview",
                                )
                    if tc.function and tc.function.arguments:
                        # ⚠️ Qwen 末尾 chunk 的 arguments=null，跳过避免 TypeError
                        if tc.function.arguments is not None:
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
                                        "arguments"
                                    ]
                                # 标记已完成解析（用于决定是否发送 tool_call_started）
                                self._current_tool_calls[tc_id]["_args_parsed"] = True
                                self._tool_calls_buffer.pop(tc_id, None)
                                # 流式中间状态：推送实际参数到 UI 更新预览
                                # 使用 buffer 中的 name 而非局部 tool_name（后续 chunk 可能不含 name 字段）
                                _buf_name = buffer["function"].get("name", tool_name)
                                self._emit_with_callback(
                                    "tool_args_updated", self.tool_args_updated, tc_id, _buf_name or "工具", parsed_args
                                )
                            except json.JSONDecodeError:
                                # 短参数的 JSON 解析失败，记录到等待队列
                                # 同时也发射长度进度，避免 UI 一直卡在"正在准备参数..."
                                prev = self._last_progress_len.get(tc_id, 0)
                                if not prev or args_len - prev >= 200:
                                    self._last_progress_len[tc_id] = args_len
                                    progress_args = {
                                        "_status": "loading",
                                        "_args_len": args_len,
                                    }
                                    # 缓冲区已有 path/file_path 时提前提取，让 UI 显示真实文件名
                                    _pm = re.search(
                                        r'"(?:path|file_path)"\s*:\s*"([^"]+)"',
                                        buffer["function"]["arguments"],
                                    )
                                    if _pm:
                                        progress_args["_path"] = _pm.group(1)
                                    _buf_name = buffer["function"].get("name", tool_name)
                                    self._emit_with_callback(
                                        "tool_args_updated",
                                        self.tool_args_updated,
                                        tc_id,
                                        _buf_name or "工具",
                                        progress_args,
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
                                progress_args = {
                                    "_status": "loading",
                                    "_args_len": args_len,
                                }
                                # 缓冲区已有 path/file_path 时提前提取
                                _pm = re.search(
                                    r'"(?:path|file_path)"\s*:\s*"([^"]+)"',
                                    buffer["function"]["arguments"],
                                )
                                if _pm:
                                    progress_args["_path"] = _pm.group(1)
                                _buf_name = buffer["function"].get("name", tool_name)
                                self._emit_with_callback(
                                    "tool_args_updated",
                                    self.tool_args_updated,
                                    tc_id,
                                    _buf_name or "工具",
                                    progress_args,
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
                    self._emit_with_callback(
                        "reasoning_content_received", self.reasoning_content_received, _reasoning_batch
                    )
                    _reasoning_batch = ""
                    _reasoning_batch_time = now

            if content:
                # 性能优化：使用 list append + join 代替字符串拼接
                self._response_chunks.append(content)
                self._response_content_blocks = append_text_block(self._response_content_blocks, content)
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
                            msvcrt = ctypes.CDLL("msvcrt.dll")
                            msvcrt._heapmin()
                            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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

        # 性能优化：移除从 worker 线程调用的 processEvents()
        # 跨线程信号传递由 Qt 的 QueuedConnection 自动处理，无需手动 processEvents
        # QCoreApplication.processEvents()

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
                            "thought_signature": buffer.get("thought_signature"),
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
                                "thought_signature": buffer.get("thought_signature"),
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

        # 流式结束后清理 index→id 临时映射（仅流处理期间需要）
        self._tool_calls_index_to_id = {}

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
            tool_call_id,
            tool_name,
            args,
            type("ToolResult", (), {"success": False, "content": None, "error": "用户中止"})(),
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

            # ====== 权限检查（统一检查所有工具，包括 question）======
            should_continue = self._check_permission(tool_name, arguments, tool_call_id, round_id, results)
            if not should_continue:
                return None  # 取消
            if results and results[-1] and results[-1].get("tool_call_id") == tool_call_id:
                continue  # 权限拒绝，跳过执行

            # ====== 处理 question 工具 ======
            if tool_name == "question":
                return self._handle_question_tool(tool_call_id, arguments)

            # ====== 执行工具 ======
            result_obj, result_content, success = self._execute_tool(tool_name, arguments, tool_call_id)
            if result_obj is self._TOOL_CANCELLED:
                return None

            # ====== 发射结果 ======
            self._emit_with_callback(
                "tool_result_received", self.tool_result_received, tool_call_id, tool_name, arguments, result_obj
            )
            results.append(
                self._build_result_dict(
                    tool_call_id, tool_name, arguments, result_content, success, round_id, result_obj
                )
            )

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
                parsed, err_result = self._parse_tool_arguments(
                    arguments_str, tool_name, arguments_str, tool_call_id, round_id
                )
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
        # 使用模块级共享线程池 —— 避免每次调用都创建/销毁 ThreadPoolExecutor 的开销
        # 共享池 max_workers=8（上限），lazy 创建线程，空闲时不消耗 OS 线程资源
        parallel_results = []
        executor = _SHARED_TOOL_POOL

        future_map = {}
        for idx, tool_name, tool_call_id, arguments, round_id in tasks:
            future = executor.submit(
                self._execute_one_tool_parallel,
                tool_name,
                tool_call_id,
                arguments,
                round_id,
                idx,
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
                pending,
                timeout=0.5,
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
        logger.warning(
            f"[ToolCall] ⚠️ JSON 解析失败且无法修复，tool={tool_name}, error={error_msg}, preview='{raw_args[:200]}...'"
        )
        preview_args = {"_raw_args": raw_args[:500], "_status": "parse_failed"}
        self._emit_tool_started(tool_call_id, tool_name, preview_args, round_id)
        error_result = {
            "success": False,
            "content": None,
            "error": f"[参数错误] JSON 格式无效: {error_msg}\n"
            f"工具: {tool_name}\n原始内容(前500字): {raw_args[:500]}...",
        }
        self._emit_with_callback(
            "tool_result_received", self.tool_result_received, tool_call_id, tool_name, preview_args, error_result
        )
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "arguments": {"_raw_args": raw_args[:500]},
            "content": error_result["error"],
            "success": False,
            "round_id": round_id,
        }

    def _check_required_args(self, tool_name, arguments, tool_call_id, round_id, original_args_str):
        """检查必需参数，缺失则返回错误结果，否则返回 None"""
        required_args = self.tool_executor.REQUIRED_ARGS.get(tool_name, [])
        missing_args = [p for p in required_args if p not in arguments]
        if missing_args:
            logger.warning(f"[ToolCall] ⚠️ 缺少必需参数: tool={tool_name}, missing={missing_args}")
            error_result = {
                "success": False,
                "content": None,
                "error": f"[参数缺失] 缺少必需参数: {missing_args}\n工具: {tool_name}",
            }
            self._emit_with_callback(
                "tool_result_received", self.tool_result_received, tool_call_id, tool_name, arguments, error_result
            )
            return {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": f"Error: Missing required arguments: {missing_args}",
                "round_id": round_id,
            }
        return None

    def _emit_tool_started(self, tool_call_id, tool_name, arguments, round_id):
        """发射 tool_call_started 信号"""
        if self.tool_start_callback:
            self.tool_start_callback(tool_call_id, tool_name, arguments, round_id)
        else:
            self._emit_with_callback(
                "tool_call_started", self.tool_call_started, tool_call_id, tool_name, arguments, round_id
            )

    def _handle_question_tool(self, tool_call_id, arguments):
        """处理 question 工具调用（阻塞等待用户回答）"""
        questions = arguments.get("questions", [])
        if not questions and "question" in arguments:
            questions = [
                {
                    "question": arguments["question"],
                    "options": arguments.get("options", []),
                    "multiple": arguments.get("multiple", False),
                }
            ]
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

        if permission_result == "deny":
            # 🛡️ 工具被关闭/禁用：直接拒绝执行，追加错误结果，通知 UI
            logger.info(f"[Permission] tool={tool_name} 被拒绝（工具开关关闭）")
            self._emit_with_callback(
                "tool_result_received",
                self.tool_result_received,
                tool_call_id,
                tool_name,
                arguments,
                type("ToolResult", (), {"success": False, "error": f"Tool '{tool_name}' is disabled by tool toggle"})(),
            )
            results.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": f"Error: Tool '{tool_name}' is disabled (set tool_toggles to enable)",
                    "round_id": round_id,
                }
            )
            return True  # 已处理，继续对话（工具不会真正执行）

        if permission_result == "ask":
            # 串行化用户交互：同一时间只有一个工具需要用户确认
            with self._permission_serializer:
                # 再次检查缓存（可能已被其他线程处理）
                if self._permission_cache.is_allowed(tool_name):
                    return True
                self._emit_with_callback(
                    "permission_approval_requested",
                    self.permission_approval_requested,
                    tool_call_id,
                    tool_name,
                    arguments,
                )
                self._permission_pending = {
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "arguments": arguments,
                }
                self._permission_approved = False
            # 锁在 while 循环前释放，避免阻塞主线程调用 approve_permission/deny_permission
            # 性能优化：移除后台线程中的 processEvents()
            # processEvents() 在非主线程调用会导致信号丢失、死锁等问题
            # UI 响应应通过信号-槽机制自然处理
            while (
                self._permission_pending is not None and not self._is_cancelled and not self._tool_execution_cancelled
            ):
                # if not self._legacy_direct_callbacks:
                #     QApplication.processEvents()  # 移除：后台线程不应调用 processEvents()
                time.sleep(0.1)  # 保留 sleep 用于轮询等待用户授权

            if self._is_cancelled or self._tool_execution_cancelled:
                self._emit_cancelled_tool_result(
                    {"id": tool_call_id, "function": {"name": tool_name, "arguments": "{}"}}
                )
                return False

            if not self._permission_approved:
                self._emit_with_callback(
                    "tool_result_received",
                    self.tool_result_received,
                    tool_call_id,
                    tool_name,
                    arguments,
                    type("ToolResult", (), {"success": False, "error": "Permission denied by user"})(),
                )
                results.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": "Error: Permission denied by user",
                        "round_id": round_id,
                    }
                )
                return True  # 已处理，继续（但不会执行工具）

        return True  # 允许执行

    def _execute_tool(self, tool_name, arguments, tool_call_id):
        """执行单个工具调用。"""
        try:
            result = self.tool_executor.execute(tool_name, arguments, call_id=tool_call_id)
        except Exception as e:
            logger.exception(f"[Tool] Tool '{tool_name}' execution failed: {e}")
            return None, f"Tool execution error: {str(e)}", False

        if self._is_cancelled or self._tool_execution_cancelled:
            self._emit_with_callback(
                "tool_result_received",
                self.tool_result_received,
                tool_call_id,
                tool_name,
                arguments,
                {"success": False, "content": None, "error": "用户中止"},
            )
            return self._TOOL_CANCELLED, None, None

        result_content = str(result) if result else ""
        success = bool(getattr(result, "success", True)) if result else False
        return result, result_content, success

    def _build_result_dict(self, tool_call_id, tool_name, arguments, result_content, success, round_id, result_obj):
        """构建标准的结果字典

        content: 字符串形式（兼容下游 consumers）
        raw_content: ToolResult.content 原始值（dict/list 等），
                     供 _try_inject_vision_content 等需要结构化数据的场景使用
        """
        raw_content = getattr(result_obj, "content", None) if result_obj else None

        # ---- 根据模型能力为 screenshot 工具结果追加提示 ----
        # 目的：防止 LLM 反复截图。程序侧知道模型是否支持视觉，而 LLM 不知道。
        # - 视觉模型：告知截图已自动以图片形式注入上下文，无需重复截图
        # - 非视觉模型：告知本模型无法看到图像内容，截图只能获得文件路径
        if tool_name == "screenshot" and success:
            try:
                from app.core.model_capabilities import get_model_capabilities
                _model_name = str(self.llm_config.get("模型名称", "") or "")
                _caps = get_model_capabilities(_model_name)
                if _caps.get("supports_vision"):
                    result_content = str(result_content) + (
                        "\n\n[Vision Notice] 截图已自动以图片形式注入你的视觉上下文，"
                        "你现在已经能看到屏幕内容了，不需要再次截图。"
                    )
                else:
                    result_content = str(result_content) + (
                        "\n\n[Notice] 本模型不支持视觉识别，你无法从截图中看到图像内容，"
                        "只能获得文件路径。"
                    )
            except Exception:
                pass  # 降级：不加额外提示

        result = {
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
            "image_data": getattr(result_obj, "image_data", None) if result_obj else None,
            # LSP 诊断文本：to_api_message 会拼接到 content 末尾供 LLM 当前轮次查看，
            # normalize_message 不保留此字段，session 历史消息不含诊断文本
            "lsp_diagnostic": getattr(result_obj, "lsp_diagnostic", None) if result_obj else None,
        }
        if raw_content is not None:
            result["raw_content"] = raw_content
        return result

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
            result_obj, result_content, success = self._execute_tool(tool_name, arguments, tool_call_id)
            if result_obj is self._TOOL_CANCELLED:
                return None

            # ====== 发射结果信号（非关键，失败不阻断） ======
            try:
                self._emit_with_callback(
                    "tool_result_received", self.tool_result_received, tool_call_id, tool_name, arguments, result_obj
                )
            except Exception as e:
                logger.warning(f"[ToolCall] 发射 tool_result_received 失败: {e}")

            return self._build_result_dict(
                tool_call_id, tool_name, arguments, result_content, success, round_id, result_obj
            )

        except Exception as e:
            logger.error(f"[ToolCall] 并行工具执行异常: tool={tool_name}, error={e}")
            return self._build_result_dict(
                tool_call_id, tool_name, arguments, f"Tool execution error: {str(e)}", False, round_id, None
            )

    def _handle_error(self, error):
        from openai import (
            APIConnectionError,
            APIError,
            APITimeoutError,
            BadRequestError,
            RateLimitError,
        )

        error_msg = str(error)

        if "peer closed connection" in error_msg.lower() or "incomplete chunked read" in error_msg.lower():
            self._emit_with_callback(
                "error_occurred",
                self.error_occurred,
                "[连接中断] 服务器在响应中途关闭了连接，可能是服务器过载或网络不稳定。请稍后重试。",
            )
            return
        if "ProtocolError" in error_msg or "RemoteProtocolError" in error_msg:
            self._emit_with_callback(
                "error_occurred", self.error_occurred, "[连接错误] 网络协议错误，可能是服务器关闭了连接。请稍后重试。"
            )
            return

        if isinstance(error, BadRequestError):
            # ⚠️ Qwen/DashScope 服务端拒绝"重复工具调用"错误
            # 错误码：InternalError.Algo.InvalidParameter
            # 错误消息：Repetitive tool calls detected in the conversation history...
            # 服务端会拒绝"连续多轮相同 (name, arguments) 的工具调用"，
            # 同样的请求序列重试仍会被拒，必须用户介入（修改输入/工具策略）。
            if any(
                p in error_msg
                for p in (
                    "repetitive tool calls detected",
                    "repetitive tool calls",
                    "identical name and arguments has been repeated",
                    "tool call with identical name",
                    "internalerror.algo.invalidparameter",
                )
            ):
                self._emit_with_callback(
                    "error_occurred",
                    self.error_occurred,
                    f"[工具调用循环] 模型在最近几轮反复用相同参数调用同一工具，"
                    f"Qwen/DashScope 服务端主动拒绝了请求（HTTP 400 InternalError.Algo.InvalidParameter）。\n\n"
                    f"原始错误：{error_msg[:300]}\n\n"
                    f"建议：\n"
                    f"  1. 修改输入，明确告诉模型何时该停止调用工具\n"
                    f"  2. 检查工具实现，确保每次返回结果有变化或包含终止信号\n"
                    f"  3. 重新发起任务（如开启新会话）",
                )
                return
            # 检测参数过大相关错误（不同 provider 的不同错误信息）
            if any(
                kw in error_msg.lower()
                for kw in ["too large", "too long", "exceeds", "maximum length", "413", "payload", "request entity"]
            ):
                self._emit_with_callback(
                    "error_occurred",
                    self.error_occurred,
                    f"[参数过长] 请求参数超出 provider 限制。\n"
                    f"这可能是工具调用的参数（如 write 的 content）过长导致的。\n"
                    f"建议: 减少一次性写入的内容长度，分批写入。\n"
                    f"详情: {error_msg[:300]}",
                )
            elif "json" in error_msg.lower() or "format" in error_msg.lower():
                self._emit_with_callback(
                    "error_occurred", self.error_occurred, f"[JSON格式错误] 请确保输入有效的JSON格式: {error_msg}"
                )
            elif "tool" in error_msg.lower() and any(
                kw in error_msg.lower() for kw in ["argument", "parameter", "missing", "required"]
            ):
                self._emit_with_callback(
                    "error_occurred",
                    self.error_occurred,
                    f"[工具参数错误] 模型生成的工具参数不完整或格式错误。\n"
                    f"建议: 重新发送请求重试。\n"
                    f"详情: {error_msg[:300]}",
                )
            else:
                self._emit_with_callback("error_occurred", self.error_occurred, f"[请求错误] {error_msg}")
        elif isinstance(error, RateLimitError):
            self._emit_with_callback(
                "error_occurred", self.error_occurred, f"[速率限制] 请求过于频繁，请稍后再试。详情: {error_msg}"
            )
        elif isinstance(error, APIConnectionError):
            self._emit_with_callback(
                "error_occurred",
                self.error_occurred,
                f"[连接失败] 无法连接到 API 服务器，请检查网络或 API_URL 设置。详情: {error_msg}",
            )
        elif isinstance(error, APITimeoutError):
            self._emit_with_callback(
                "error_occurred",
                self.error_occurred,
                f"[超时] 请求超时（300秒），请检查网络或模型负载。详情: {error_msg}",
            )
        elif isinstance(error, APIError):
            if "context length" in error_msg and "overflow" in error_msg:
                self._emit_with_callback(
                    "error_occurred",
                    self.error_occurred,
                    f"[上下文超限] 输入内容过长，请缩短对话或清除历史记录。详情: {error_msg}",
                )
            elif "insufficient_quota" in error_msg:
                self._emit_with_callback(
                    "error_occurred", self.error_occurred, "[配额不足] API配额已用完，请检查账户余额或更换API Key。"
                )
            else:
                self._emit_with_callback("error_occurred", self.error_occurred, f"[API错误] {error_msg}")
        elif "unrecognized_parameter" in error_msg or "extra_parameters" in error_msg:
            self._emit_with_callback(
                "error_occurred",
                self.error_occurred,
                f"[兼容性提示] 当前模型可能不支持某些高级设置（如思考模式或温度）。错误: {error_msg}",
            )
        elif "max_tokens" in error_msg.lower() or "context length" in error_msg.lower():
            self._emit_with_callback(
                "error_occurred",
                self.error_occurred,
                "[错误] 模型上下文或最大Token超出限制，请减少输入长度或调低 max_tokens",
            )
        elif "authentication" in error_msg.lower() or "api key" in error_msg.lower():
            self._emit_with_callback(
                "error_occurred", self.error_occurred, "[认证错误] API Key无效或已过期，请检查配置。"
            )
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
            colon = raw_str.find(":", content_start)
            if colon >= 0:
                # 找到 content 值的起始引号
                first_quote = raw_str.find('"', colon)
                if first_quote >= 0:
                    content_begin = first_quote + 1
                    # 尝试找闭合引号（考虑转义）
                    content_end = -1
                    i = content_begin
                    while i < len(raw_str):
                        if raw_str[i] == "\\" and i + 1 < len(raw_str):
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
            colon = raw_str.find(":", ops_start)
            if colon >= 0:
                arr_start = raw_str.find("[", colon)
                if arr_start >= 0:
                    # 找匹配的 ]（考虑嵌套）
                    depth = 0
                    arr_end = -1
                    for i in range(arr_start, len(raw_str)):
                        if raw_str[i] == "[":
                            depth += 1
                        elif raw_str[i] == "]":
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
    if _sys.argv[0].endswith("pytest") or "PYTEST_CURRENT_TEST" in os.environ:
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
        if _sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            heap = _call(kernel32.GetProcessHeap)
            if not heap:
                return
            _call(kernel32.HeapCompact, heap, 0)
            # freed 返回值不稳定，不 try to log it

        elif _sys.platform == "linux":
            try:
                import ctypes

                libc = ctypes.CDLL("libc.so.6", use_last_error=True)
                if _call(libc.malloc_trim, 0) != 0:
                    logger.info("[MEM-HEAP] Linux malloc_trim(0) 释放了空闲堆内存")
            except Exception:
                pass

        elif _sys.platform == "darwin":
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
