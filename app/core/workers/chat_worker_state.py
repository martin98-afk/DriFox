# -*- coding: utf-8 -*-
"""
ChatWorker 状态管理 - 使用 dataclass 统一定义所有状态

将分散的私有字段封装为统一的结构，便于：
1. 状态生命周期管理
2. 测试时 mock 特定状态
3. 清理逻辑集中化
"""

from collections import deque
from dataclasses import dataclass, field
from threading import Event
from typing import Dict, List, Any, Optional, Set


@dataclass
class ApiCacheState:
    """API 消息缓存状态"""
    cache: Optional[List[Dict[str, Any]]] = None  # 缓存的 API 消息
    built: bool = False  # 是否已完成初始构建
    current_api_messages: List = field(default_factory=list)
    current_system_prompt: str = ""


@dataclass
class ToolCallState:
    """工具调用状态"""
    current_calls: Dict[str, Any] = field(default_factory=dict)  # tool_call_id -> call info
    calls_buffer: Dict[str, Any] = field(default_factory=dict)  # tool_call_id -> buffer
    waiting_params: Dict[str, dict] = field(default_factory=dict)  # tool_call_id -> {buffer, attempt_count}
    previewed_ids: Set[str] = field(default_factory=set)  # 已预览的 tool_call_id
    execution_cancelled: bool = False


@dataclass
class ResponseContentState:
    """响应内容状态"""
    full_response: str = ""
    reasoning_content: str = ""
    reasoning_chunks: deque = field(default_factory=lambda: deque())  # 思考内容片段
    content_blocks: List = field(default_factory=list)  # 响应内容块
    response_chunks: deque = field(default_factory=lambda: deque())  # 响应片段
    last_progress_len: Dict[str, int] = field(default_factory=dict)  # tc_id -> 上一次推送进度时的字符数


@dataclass
class CompactionState:
    """上下文压缩状态"""
    cache: Dict = field(default_factory=dict)  # 压缩缓存
    last_state: Dict = field(default_factory=lambda: {
        "active": False,
        "source": "worker",
        "kind": "",
        "original_count": 0,
        "summarized_count": 0,
        "kept_count": 0,
        "summary_count": 0,
        "note": "",
    })


@dataclass
class PermissionState:
    """权限状态"""
    pending_question: Optional[Dict] = None  # 待回答的问题 {"question", "options", "multiple"}
    pending_answer: Optional[str] = None  # 待回答的答案
    answer_event: Event = field(default_factory=Event)  # 回答事件
    pending_permission: Optional[Dict] = None  # 待批准的权限
    permission_approved: bool = False


@dataclass
class HttpClientState:
    """HTTP 客户端状态"""
    client: Optional[Any] = None  # 复用的 OpenAI 客户端
    cached_config: Optional[Dict[str, Any]] = None  # 缓存的 API 配置
    max_param_retry_count: int = 10  # 最多重试10次


@dataclass
class SessionState:
    """会话状态"""
    current_messages: List[Dict] = field(default_factory=list)  # 当前会话消息
    last_usage: Optional[Dict] = None  # 上一次 API 调用的 token usage
    accumulated_tokens: int = 0  # 累加的总 token 使用量


@dataclass
class ChatWorkerState:
    """
    ChatWorker 所有状态的容器
    
    使用 dataclass 统一定义，便于：
    - 初始化：传入原始参数，构建完整状态
    - 清理：reset() 方法重置所有状态
    - 测试：可以单独 mock 某个状态类
    """
    # === 构造函数参数（只读）===
    initial_messages: List[Dict] = field(default_factory=list)
    initial_session_messages: List[Dict] = field(default_factory=list)
    llm_config: Dict = field(default_factory=dict)
    tools: List = field(default_factory=list)
    compaction_prompt: str = ""
    compaction_config: Dict = field(default_factory=dict)
    
    # === 状态分组 ===
    api_cache: ApiCacheState = field(default_factory=ApiCacheState)
    tool_call: ToolCallState = field(default_factory=ToolCallState)
    response: ResponseContentState = field(default_factory=ResponseContentState)
    compaction: CompactionState = field(default_factory=CompactionState)
    permission: PermissionState = field(default_factory=PermissionState)
    http: HttpClientState = field(default_factory=HttpClientState)
    session: SessionState = field(default_factory=SessionState)
    
    # === 状态标志 ===
    is_cancelled: bool = False
    
    # === 权限缓存 ===
    permission_cache: Any = None  # PermissionCache 实例
    
    # === 事件总线（不清理）===
    event_bus: Any = None
    
    # === 工具执行器引用 ===
    tool_executor: Any = None
    compactor: Any = None
    
    @classmethod
    def from_constructor_args(
        cls,
        messages: List[Dict],
        session_messages: List[Dict],
        llm_config: Dict,
        tools: List[Dict],
        compaction_prompt: str,
        compaction_config: Dict,
        permission_cache: Any,
        event_bus: Any,
        tool_executor: Any,
        compactor: Any,
        initial_compaction_cache: Dict,
    ) -> "ChatWorkerState":
        """从构造函数参数构建状态"""
        return cls(
            initial_messages=messages,
            initial_session_messages=session_messages,
            llm_config=llm_config,
            tools=tools or [],
            compaction_prompt=compaction_prompt,
            compaction_config=compaction_config or {},
            api_cache=ApiCacheState(
                cache=None,
                built=False,
            ),
            tool_call=ToolCallState(),
            response=ResponseContentState(),
            compaction=CompactionState(
                cache=initial_compaction_cache or {},
                last_state={
                    "active": False,
                    "source": "worker",
                    "kind": "",
                    "original_count": len(messages or []),
                    "summarized_count": 0,
                    "kept_count": len(messages or []),
                    "summary_count": 0,
                    "note": "",
                },
            ),
            permission=PermissionState(),
            http=HttpClientState(),
            session=SessionState(
                current_messages=list(session_messages or []),
            ),
            permission_cache=permission_cache,
            event_bus=event_bus,
            tool_executor=tool_executor,
            compactor=compactor,
        )
    
    def reset_pending_response_state(self):
        """
        重置单轮对话结束后的中间状态。
        在每次 API 调用前和工具执行完成后调用，释放内存。
        
        注意：必须同时清除 response_chunks，否则流式文本 chunk 
        会跨迭代累积，导致 _response_chunks deque 的内存泄漏。
        """
        # 工具调用状态
        self.tool_call.current_calls = {}
        self.tool_call.calls_buffer = {}
        self.tool_call.waiting_params = {}
        self.tool_call.previewed_ids = set()
        
        # 响应内容（但保留 full_response）
        self.response.content_blocks = []
        self.response.reasoning_content = ""
        self.response.reasoning_chunks = deque()
        # 🔧 修复：清除流式文本 chunks，防止跨迭代累积
        self.response.response_chunks = deque()
        self.response.last_progress_len = {}
    
    def full_cleanup(self):
        """
        彻底清理所有状态，防止内存泄漏。
        在对话结束后调用。

        注意：不重置 is_cancelled / execution_cancelled 取消标志。
        原因：finalize_stop() 可能因 worker 被工具线程阻塞而超时，
        此时调用 cleanup() 后 worker 线程仍可能恢复执行。
        如果重置取消标志，worker 会误以为未被取消而继续发起 API 调用
        （用旧会话的消息浪费 tokens）。保持取消标志不变确保 worker
        恢复后立刻退出循环。
        """
        # 重置工具调用
        self.tool_call = ToolCallState()
        
        # 重置响应内容
        self.response = ResponseContentState()
        
        # 重置权限
        self.permission = PermissionState()
        
        # 重置 HTTP 客户端
        self.http = HttpClientState()
        
        # 重置 API 缓存
        self.api_cache = ApiCacheState()
        
        # 清空会话消息
        self.session = SessionState()
        
        # 🔧 修复：清空 EventBus 订阅者，防止 handler 闭包引用残留
        if self.event_bus is not None:
            try:
                self.event_bus.clear()
            except Exception:
                pass
            self.event_bus = None
        
        # 清空工具列表引用
        self.tools = []