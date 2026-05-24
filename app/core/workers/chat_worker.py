# -*- coding: utf-8 -*-
"""
Chat Worker - OpenAI 对话执行器
"""

import re
import time
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
)

from app.constants import PARAM_SCHEMA
from app.core.conversation.config import PermissionCache
from app.core.message_content import consolidate_messages, append_text_block, messages_to_api, to_api_message
from app.core.provider_profile import get_provider_profile
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
        self._event_bus.emit(event, *args, **kwargs)

    def _emit_with_callback(self, signal_name: str, signal, *args) -> None:
        """发射信号并尝试直接回调（已废弃，推荐使用 _emit_via_event_bus）

        兼容旧接口，内部使用事件总线分发事件。

        Args:
            signal_name: 信号名（用于查找直接回调）
            signal: Qt 信号对象（保留用于向后兼容）
            *args: 传递给回调/信号的参数
        """
        # 映射 signal_name 到 WorkerEvent
        event = self._signal_name_to_event(signal_name)
        if event:
            self._emit_via_event_bus(event, *args)

        # 向后兼容：仍然发射 PyQt Signal（UI 层依赖）
        if signal is not None:
            signal.emit(*args)

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

        # 清理 HTTP 客户端缓存
        self._http_client = None
        self._cached_api_config = None

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
        config_key = str(self.llm_config.get("API_KEY", "")) + str(self.llm_config.get("API_URL", ""))

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
            if cn_key in ["API_KEY", "API_URL", "模型名称", "系统提示", "启用技能"]:
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

        # 处理服务商特有的思考模式参数
        profile = get_provider_profile(self.llm_config)
        provider_family = profile["family"]

        if provider_family == "deepseek":
            thinking_mode = self.llm_config.get("思考模式")
            if thinking_mode is True:
                extra_body["thinking"] = {"type": "enabled"}
                # reasoning_effort 由映射自动流入，无需额外处理
            elif thinking_mode is False:
                extra_body["thinking"] = {"type": "disabled"}
                # API 要求：关闭思考时不能传 reasoning_effort
                extra_body.pop("reasoning_effort", None)

        elif provider_family == "zhipu":
            thinking_mode = self.llm_config.get("思考模式")
            if thinking_mode is True:
                extra_body["thinking"] = {"type": "enabled"}
            elif thinking_mode is False:
                extra_body["thinking"] = {"type": "disabled"}
            # 智谱AI 不支持 reasoning_effort，已有映射不会注入

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
            while not self._is_cancelled:
                if self._is_cancelled:
                    return

                # 每次 API 调用前：1. 清理中间状态  2. 检查压缩
                self._clear_pending_response_state()

                # 使用 API 消息缓存（首次会重建，后续复用）
                tool_calls_found, tool_args_pending = self._make_api_call(current_messages, use_cache=True)
                if self._is_cancelled:
                    return
                if tool_calls_found and tool_args_pending:
                    continue
                if self._is_cancelled:
                    return

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
                    question_result = {
                        "role": "tool",
                        "tool_call_id": q["tool_call_id"],
                        "name": "question",
                        "arguments": {},
                        "content": self._pending_answer,
                        "success": True,
                    }
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

                response_sequence = self._build_response_message_sequence(tool_results)
                current_messages.extend(response_sequence)
                current_session_messages.extend(response_sequence)
                self._current_session_messages = list(current_session_messages)
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
                    self._api_messages_cache = messages_to_api(current_messages)
                    # 注意：current_session_messages 故意不做同步压缩。
                    # 它的增长会在 worker 结束时由 _on_messages_updated 的
                    # preserve_compaction=False 清空缓存，下轮发送时由 ContextBudgetAllocator 统一压缩。
                else:
                    # 更新 API 消息缓存
                    self._append_to_api_cache(response_sequence)
                self._emit_with_callback("finished_with_messages", self.finished_with_messages,
                                         current_session_messages)

                QCoreApplication.processEvents()
                time.sleep(0.01)

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
                        logger.warning(f"[API] 已修复消息顺序，已更新缓存，重试 (attempt {attempt + 1}/{max_retries})")
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

                should_retry = (
                        is_rate_limit or is_server_overload or is_conn_error or
                        is_retryable_network or is_retryable_timeout or is_retryable_protocol
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
        return self._process_response(response)

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
        for chunk in response:
            if self._is_cancelled:
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
                                self._emit_with_callback(
                                    "tool_args_updated", self.tool_args_updated,
                                    tc_id, tool_name, parsed_args
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
                                    self._emit_with_callback(
                                        "tool_args_updated", self.tool_args_updated,
                                        tc_id, tool_name, progress_args
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
                                self._emit_with_callback(
                                    "tool_args_updated", self.tool_args_updated,
                                    tc_id, tool_name, progress_args
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
            if chunk_count % 5 == 0:
                QCoreApplication.processEvents()

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

        return tool_calls_found, tool_args_pending

    def _execute_all_tools(self):
        if not self._current_tool_calls or not self.tool_executor:
            return []

        # 重置工具执行取消标志，开始新的执行周期
        self._tool_execution_cancelled = False

        results = []
        for tc in self._current_tool_calls.values():
            if self._is_cancelled or self._tool_execution_cancelled:
                cancelled_tc_id = tc["id"]
                tool_name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except:
                    args = {}
                self._emit_with_callback(
                    "tool_result_received",
                    self.tool_result_received,
                    cancelled_tc_id,
                    tool_name,
                    args,
                    type(
                        "ToolResult",
                        (),
                        {"success": False, "content": None, "error": "用户中止"},
                    )(),
                )
                if not self._legacy_direct_callbacks:
                    QApplication.processEvents()
                # 不设置 _is_cancelled = True，让 worker 继续下次迭代
                # 返回空列表而非 None，避免进入问答等待逻辑
                return []

            tool_name = tc["function"]["name"]
            arguments = tc["function"]["arguments"]
            tool_call_id = tc["id"]
            raw_args = tc["function"]["arguments"]
            round_id = f"round_{id(tc)}"
            original_args_str = arguments  # 保存原始字符串用于错误诊断

            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as e:
                    # JSON 解析失败，尝试智能修复（处理模型生成的不规范 JSON）
                    fixed_args = smart_parse_arguments(arguments, tool_name)
                    if fixed_args is not None:
                        arguments = fixed_args
                        logger.info(
                            f"[ToolCall] ✓ JSON 智能修复成功: tool={tool_name}, "
                            f"args={list(arguments.keys())}"
                        )
                    elif not arguments or not arguments.strip():
                        # 确实是空字符串
                        arguments = {}
                    else:
                        # 【优化】尝试三阶段修复（不再对 10000 字符设硬限制）
                        # 阶段1: 再次调用 smart_parse_arguments（重试一次）
                        retry_fixed = smart_parse_arguments(arguments, tool_name)
                        if retry_fixed is not None:
                            arguments = retry_fixed
                            logger.info(
                                f"[ToolCall] ✓ 二次 JSON 智能修复成功: tool={tool_name}, "
                                f"args_len={len(arguments)}, args={list(retry_fixed.keys())}"
                            )
                        else:
                            # 阶段2: 对于 write/edit 工具，尝试从原始字符串提取关键参数
                            if tool_name in ("write", "edit"):
                                extracted = _extract_tool_args_from_raw(arguments, tool_name)
                                if extracted:
                                    arguments = extracted
                                    logger.info(
                                        f"[ToolCall] ✓ 原始参数提取成功: tool={tool_name}, "
                                        f"keys={list(extracted.keys())}"
                                    )
                                else:
                                    # 阶段3: 彻底失败
                                    logger.warning(
                                        f"[ToolCall] ⚠️ JSON 解析失败且无法修复，tool={tool_name}, "
                                        f"error={str(e)}, "
                                        f"args_len={len(arguments)}, "
                                        f"preview='{arguments[:200]}...'"
                                    )
                                    preview_args = {"_raw_args": raw_args[:500], "_status": "parse_failed"}
                                    if self.tool_start_callback:
                                        self.tool_start_callback(tool_call_id, tool_name, preview_args, round_id)
                                    else:
                                        self._emit_with_callback(
                                            "tool_call_started",
                                            self.tool_call_started,
                                            tool_call_id, tool_name, preview_args, round_id
                                        )
                                    error_result = {
                                        "success": False,
                                        "content": None,
                                        "error": f"[参数错误] JSON 格式无效: {str(e)}\n"
                                                 f"工具: {tool_name}\n"
                                                 f"原始内容(前500字): {arguments[:500]}...\n"
                                                 f"提示: 可尝试分批执行，或使用 patch 工具替代 write。",
                                    }
                                    self._emit_with_callback(
                                        "tool_result_received",
                                        self.tool_result_received,
                                        tool_call_id, tool_name, preview_args,
                                        error_result
                                    )
                                    if not self._legacy_direct_callbacks:
                                        QApplication.processEvents()
                                    results.append({
                                        "role": "tool",
                                        "tool_call_id": tool_call_id,
                                        "name": tool_name,
                                        "arguments": {"_raw_args": raw_args[:500]},
                                        "content": error_result["error"],
                                        "success": False,
                                        "round_id": round_id,
                                    })
                                    continue
                            else:
                                # 非文件工具，无法修复
                                logger.warning(
                                    f"[ToolCall] ⚠️ JSON 解析失败且无法修复，tool={tool_name}, "
                                    f"error={str(e)}, "
                                    f"preview='{arguments[:200]}...'"
                                )
                                preview_args = {"_raw_args": raw_args[:500], "_status": "parse_failed"}
                                if self.tool_start_callback:
                                    self.tool_start_callback(tool_call_id, tool_name, preview_args, round_id)
                                else:
                                    self._emit_with_callback(
                                        "tool_call_started",
                                        self.tool_call_started,
                                        tool_call_id, tool_name, preview_args, round_id
                                    )
                                error_result = {
                                    "success": False,
                                    "content": None,
                                    "error": f"[参数错误] JSON 格式无效: {str(e)}\n"
                                             f"工具: {tool_name}\n"
                                             f"原始内容(前500字): {arguments[:500]}...",
                                }
                                self._emit_with_callback(
                                    "tool_result_received",
                                    self.tool_result_received,
                                    tool_call_id, tool_name, preview_args,
                                    error_result
                                )
                                if not self._legacy_direct_callbacks:
                                    QApplication.processEvents()
                                results.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call_id,
                                    "name": tool_name,
                                    "arguments": {"_raw_args": raw_args[:500]},
                                    "content": error_result["error"],
                                    "success": False,
                                    "round_id": round_id,
                                })
                                continue

            # 检查必需参数
            required_args = self.tool_executor.REQUIRED_ARGS.get(tool_name, [])
            missing_args = [p for p in required_args if p not in arguments]
            if missing_args:
                logger.warning(
                    f"[ToolCall] ⚠️ 缺少必需参数: tool={tool_name}, missing={missing_args}, "
                    f"raw_args='{original_args_str if isinstance(original_args_str, str) else str(original_args_str)}'"
                )
                tool_call_id = tc["id"]
                round_id = f"round_{id(tc)}"
                error_result = {
                    "success": False,
                    "content": None,
                    "error": f"[参数缺失] 缺少必需参数: {missing_args}\n"
                             f"工具: {tool_name}",
                }
                self._emit_with_callback(
                    "tool_result_received",
                    self.tool_result_received,
                    tool_call_id, tool_name, arguments,
                    error_result
                )
                if not self._legacy_direct_callbacks:
                    QApplication.processEvents()
                results.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "arguments": arguments,
                    "content": error_result["error"],
                    "success": False,
                    "round_id": round_id,
                })
                continue

            tool_call_id = tc["id"]

            round_id = f"round_{id(tc)}"
            if self.tool_start_callback:
                self.tool_start_callback(tool_call_id, tool_name, arguments, round_id)
            else:
                self._emit_with_callback(
                    "tool_call_started",
                    self.tool_call_started,
                    tool_call_id, tool_name, arguments, round_id
                )
                if not self._legacy_direct_callbacks:
                    QApplication.processEvents()

            if tool_name == "question":
                questions = arguments.get("questions", [])
                # 向后兼容旧格式
                if not questions and "question" in arguments:
                    questions = [{
                        "question": arguments["question"],
                        "options": arguments.get("options", []),
                        "multiple": arguments.get("multiple", False),
                    }]
                # 规范化 questions 中的选项格式
                for q in questions:
                    opts = q.get("options", [])
                    normalized = []
                    for opt in opts:
                        if isinstance(opt, str):
                            normalized.append({"label": opt, "description": ""})
                        elif isinstance(opt, dict):
                            normalized.append({
                                "label": opt.get("label", opt.get("name", str(opt))),
                                "description": opt.get("description", ""),
                            })
                        else:
                            normalized.append({"label": str(opt), "description": ""})
                    q["options"] = normalized
                extra = {"tool_call_id": tool_call_id}
                self._emit_with_callback("question_asked", self.question_asked, tool_call_id, questions, extra)
                self._question_pending = {
                    "tool_call_id": tool_call_id,
                    "questions": questions,
                }
                return None

            if self.permission_check_callback:
                # 使用 PermissionCache 检查缓存，缓存命中则直接允许
                if self._permission_cache.is_allowed(tool_name):
                    permission_result = "allow"
                    logger.info(f"[Permission] 使用缓存: tool={tool_name}")
                else:
                    permission_result = self.permission_check_callback(
                        tool_name, arguments
                    )
                if permission_result == "ask":
                    self._emit_with_callback("permission_approval_requested", self.permission_approval_requested,
                                             tool_call_id, tool_name, arguments)
                    self._permission_pending = {
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "arguments": arguments,
                    }
                    self._permission_approved = False
                    while (
                            self._permission_pending is not None
                            and not self._is_cancelled
                            and not self._tool_execution_cancelled
                    ):
                        if not self._legacy_direct_callbacks:
                            QApplication.processEvents()
                        time.sleep(0.1)

                    if self._is_cancelled or self._tool_execution_cancelled:
                        cancelled_tc_id = tool_call_id
                        self._emit_with_callback(
                            "tool_result_received",
                            self.tool_result_received,
                            cancelled_tc_id,
                            tool_name,
                            arguments,
                            type(
                                "ToolResult",
                                (),
                                {
                                    "success": False,
                                    "content": None,
                                    "error": "用户中止",
                                },
                            )(),
                        )
                        if not self._legacy_direct_callbacks:
                            QApplication.processEvents()
                        self._is_cancelled = True
                        return None

                    if not self._permission_approved:
                        self._emit_with_callback(
                            "tool_result_received",
                            self.tool_result_received,
                            tool_call_id,
                            tool_name,
                            arguments,
                            type(
                                "ToolResult",
                                (),
                                {
                                    "success": False,
                                    "error": "Permission denied by user",
                                },
                            )(),
                        )
                        if not self._legacy_direct_callbacks:
                            QApplication.processEvents()
                        results.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                                "content": "Error: Permission denied by user",
                                "round_id": round_id,
                            }
                        )
                        continue

            try:
                # 设置当前调用的 call_id（用于文件操作记录）
                if hasattr(self.tool_executor, "set_call_id"):
                    self.tool_executor.set_call_id(tool_call_id)

                # 传递取消标志引用，以便异步工具可以检测取消
                cancelled_ref = [self._is_cancelled or self._tool_execution_cancelled]
                result = self.tool_executor.execute(tool_name, arguments, cancelled_ref)
                # 更新取消标志引用
                cancelled_ref[0] = self._is_cancelled or self._tool_execution_cancelled
            except Exception as e:
                logger.error(f"[Tool] Tool '{tool_name}' execution failed: {e}")
                result = None
                result_content = f"Tool execution error: {str(e)}"
                success = False
            else:
                result_content = str(result) if result else ""
                success = bool(getattr(result, "success", True)) if result else False

            if self._is_cancelled or self._tool_execution_cancelled:
                # 创建取消结果对象（直接使用 dict 简化，避免循环导入）
                cancelled_result = {
                    "success": False,
                    "content": None,
                    "error": "用户中止",
                }
                self._emit_with_callback(
                    "tool_result_received",
                    self.tool_result_received,
                    tool_call_id, tool_name, arguments, cancelled_result
                )
                if not self._legacy_direct_callbacks:
                    QApplication.processEvents()
                self._is_cancelled = True
                return None

            self._emit_with_callback("tool_result_received", self.tool_result_received, tool_call_id, tool_name,
                                     arguments, result)
            if not self._legacy_direct_callbacks:
                QApplication.processEvents()
            results.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "arguments": arguments or {},
                    "content": result_content,
                    "success": success,
                    "round_id": round_id,
                    "diff": getattr(result, "diff", None) if result else None,
                    "anchors": getattr(result, "anchors", None) if result else None,
                }
            )

        return results

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