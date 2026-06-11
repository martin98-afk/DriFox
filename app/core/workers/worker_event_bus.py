# -*- coding: utf-8 -*-
"""
Worker 事件总线 - 统一的事件通知机制

替代 PyQt Signal + direct_callback 双重模式。
Worker 只发事件，调用方自行订阅。
"""

import threading
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, Dict, List, Any, Optional
from loguru import logger


class WorkerEvent(Enum):
    """Worker 事件类型枚举"""
    # 文本内容事件
    CONTENT_RECEIVED = auto()      # 收到文本内容片段
    REASONING_RECEIVED = auto()    # 收到思考内容（DeepSeek）
    
    # 完成事件
    FINISHED = auto()              # 对话完成（兼容旧接口）
    FINISHED_WITH_CONTENT = auto() # 对话完成（含完整文本）
    FINISHED_WITH_MESSAGES = auto() # 对话完成（含完整消息列表）
    
    # 工具调用事件
    TOOL_CALL_STARTED = auto()     # 工具调用开始
    TOOL_RESULT_RECEIVED = auto()  # 工具结果返回
    TOOL_CALL_STREAM = auto()      # 工具调用流式片段（tool_call_id, name, chunk, type）
    
    # 交互事件
    QUESTION_ASKED = auto()        # 需要用户回答问题
    PERMISSION_REQUESTED = auto()  # 需要用户授权权限
    
    # 状态事件
    COMPACTION_STATUS = auto()    # 压缩状态变化
    ERROR = auto()                 # 发生错误
    
    # 兼容旧接口（保留 signal name 映射）
    _SIGNAL_NAME_MAP = {
        CONTENT_RECEIVED: "content_received",
        REASONING_RECEIVED: "reasoning_content_received",
        FINISHED: "finished",
        FINISHED_WITH_CONTENT: "finished_with_content",
        FINISHED_WITH_MESSAGES: "finished_with_messages",
        TOOL_CALL_STARTED: "tool_call_started",
        TOOL_RESULT_RECEIVED: "tool_result_received",
        QUESTION_ASKED: "question_asked",
        PERMISSION_REQUESTED: "permission_approval_requested",
        COMPACTION_STATUS: "compaction_status_changed",
        ERROR: "error_occurred",
    }


@dataclass
class ToolCallStartedPayload:
    """工具调用开始负载"""
    tool_call_id: str
    tool_name: str
    arguments: dict
    round_id: str


@dataclass
class ToolResultPayload:
    """工具结果负载"""
    tool_call_id: str
    tool_name: str
    arguments: dict
    result: Any  # 可能是 dict 或 ErrorResult


@dataclass
class QuestionPayload:
    """问题负载"""
    tool_call_id: str
    questions: list
    extra: dict = None


@dataclass
class PermissionPayload:
    """权限请求负载"""
    tool_call_id: str
    tool_name: str
    arguments: dict


@dataclass
class CompactionPayload:
    """压缩状态负载"""
    active: bool
    source: str
    kind: str
    original_count: int
    summarized_count: int
    kept_count: int
    summary_count: int
    note: str


class WorkerEventBus:
    """Worker 事件总线 - 订阅/发布模式

    线程安全：所有公开方法使用 threading.Lock 保护 _handlers，
    防止跨线程 emit() 与 clear() 的并发数据竞争。
    """
    
    def __init__(self):
        self._handlers: Dict[WorkerEvent, List[Callable]] = {}
        self._lock = threading.Lock()
    
    def subscribe(self, event: WorkerEvent, handler: Callable) -> None:
        """订阅事件
        
        Args:
            event: 事件类型
            handler: 回调函数，签名取决于事件类型
        """
        with self._lock:
            if event not in self._handlers:
                self._handlers[event] = []
            if handler not in self._handlers[event]:
                self._handlers[event].append(handler)
    
    def unsubscribe(self, event: WorkerEvent, handler: Callable) -> None:
        """取消订阅"""
        with self._lock:
            if event in self._handlers and handler in self._handlers[event]:
                self._handlers[event].remove(handler)
    
    def emit(self, event: WorkerEvent, *args, **kwargs) -> None:
        """广播事件到所有订阅者
        
        Args:
            event: 事件类型
            *args, **kwargs: 事件数据，传递给所有 handler
        """
        with self._lock:
            if event not in self._handlers:
                return
            # 快照 handler 列表，在锁保护下拷贝，避免外部回调修改 _handlers
            handlers = list(self._handlers[event])
        for handler in handlers:
            try:
                handler(*args, **kwargs)
            except Exception as e:
                logger.error(f"[EventBus] Handler error for {event.name}: {e}")
    
    def clear(self) -> None:
        """清空所有订阅"""
        with self._lock:
            self._handlers.clear()
    
    def has_subscribers(self, event: WorkerEvent) -> bool:
        """检查是否有订阅者"""
        with self._lock:
            return event in self._handlers and len(self._handlers[event]) > 0