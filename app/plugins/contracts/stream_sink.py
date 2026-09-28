# -*- coding: utf-8 -*-
"""流式事件与接收器契约 — 协议解析的第二层（第一层为 ProtocolTransport）。

分层（详见 docs/superpowers/specs/2026-09-23-chat-responses-transport-plugin.md）：

    ProtocolTransport  响应形状 → StreamEvent     （协议插件，零 worker 状态）
    StreamEventSink    StreamEvent → worker 状态机（接收器插件，含历史修复）

设计动机：原 chat_worker._process_response（577 行）不是纯解析函数，而是 worker 状态机
的主驱动——直接读写 20+ 私有状态并驱动 6 路 Qt 信号，内含 20 处历史修复（Qwen 并行
tool_call index 回查、孤立 buffer 死锁、Gemini thought_signature 透传、截断检测等）。
故本契约把「协议形状识别」（可替换）与「状态机驱动」（默认实现原地搬迁）拆开：
协议插件只产出事件，接收器消费事件。

EVENT_TYPES 为闭集：新增事件类型属契约变更，需同步改本模块与全部 sink 实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Protocol, Set, Tuple, runtime_checkable

# 事件类型闭集（协议插件可产出、接收器须全部处理的类型）
EVENT_TYPES = frozenset(
    {
        "content_delta",  # 正文增量 → text
        "reasoning_delta",  # 思考内容增量 → text
        "tool_call_begin",  # 工具调用开始 → tool_call_id + name
        "tool_args_delta",  # 工具参数增量 → tool_call_id + text
        "tool_call_done",  # 工具调用完成 → tool_call_id + name + arguments（完整）
        "usage",  # token 统计 → usage
        "finish",  # 流结束 → finish_reason（stop/length/content_filter）
    }
)


class StreamInterruptedError(RuntimeError):
    """流式响应被服务端提前截断/过滤/异常空响应。

    用于区分「正常完成」与「服务端截断」：
    - finish_reason='length'：max_tokens 截断，回复不完整
    - finish_reason='content_filter'：内容被安全过滤
    - 收到 chunk 但无任何输出内容（无 content/reasoning/tool_calls）
    抛出后由 _handle_error 给出明确提示，避免静默当正常完成。

    契约归属：截断/过滤/空响应是 sink.consume 的异常语义（见 StreamEventSink），
    异常类型必须在契约层定义——原置于 chat_worker 迫使 sink 插件 import 5000 行
    worker 模块链（lazy import 只掩盖循环，不消除耦合）。
    """


@dataclass
class StreamEvent:
    """归一化流式事件（协议无关的中间表示）。

    字段按事件类型使用，未使用字段保持默认值：

    | type             | 使用字段                                      |
    |------------------|-----------------------------------------------|
    | content_delta    | text                                          |
    | reasoning_delta  | text                                          |
    | tool_call_begin  | tool_call_id, name                            |
    | tool_args_delta  | tool_call_id, text                            |
    | tool_call_done   | tool_call_id, name, arguments                 |
    | usage            | usage                                         |
    | finish           | finish_reason                                 |

    thought_signature：Gemini 多轮工具调用必需（缺失会 400），随 tool_call_done 透传。
    """

    type: str
    text: str = ""
    tool_call_id: str = ""
    name: str = ""
    arguments: str = ""
    finish_reason: str = ""
    usage: Dict[str, int] = field(default_factory=dict)
    thought_signature: str = ""
    # 工具调用在本次响应中的序号（部分服务商 chunk 2+ 会清空 id，靠 index 回查真实 id；
    # Qwen/DashScope 等兼容协议必需）。非工具事件保持 -1。
    index: int = -1

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"未知流式事件类型: {self.type!r}（允许: {sorted(EVENT_TYPES)}）")


@runtime_checkable
class SinkContext(Protocol):
    """worker 侧流式上下文契约（实现：app.core.workers.stream_sink_context.StreamSinkContext）。

    sink 插件只依赖本协议，不 import worker。属性名即 ctx 公开名，
    实现侧经 _ATTR_MAP 代理到 worker 私有属性。
    """

    # 运行时字段（worker 每次 consume 前 bind）
    response: Any
    stream: bool
    token_update_callback: Any
    # 回调与常量
    tool_start_callback: Any
    DEFERRED_PREVIEW_TOOLS: Set[str]
    max_param_retry_count: int
    # 流式过程状态（读写）
    response_content_blocks: List[Dict[str, Any]]
    current_tool_calls: Dict[str, Dict[str, Any]]
    tool_calls_buffer: Dict[str, Dict[str, Any]]
    tool_calls_index_to_id: Dict[Any, str]
    response_chunks: List[str]
    chunks_total_len: int
    reasoning_content: str
    reasoning_chunks: List[str]
    last_usage: Optional[Dict[str, int]]
    cache_tracker: Any
    waiting_tool_params: Dict[str, Dict[str, Any]]
    previewed_tool_call_ids: Set[str]
    last_progress_len: Dict[str, int]
    last_progress_ts: Dict[str, float]
    last_est_len: Dict[str, int]
    last_line_est: Dict[Any, Tuple[int, int]]
    is_cancelled: bool
    streaming_rss_base: float
    mem_total_chunks_logged: int
    current_response: Any
    stream_lock: Any
    last_ttft_ms: float
    llm_req_t0: float
    mem_diag_enabled: bool
    accumulated_tokens: int

    def emit(self, signal_name: str, *args: Any) -> None:
        """发射 worker 信号（信号对象由适配层查表补齐）"""
        ...

    def get_reasoning_content(self) -> str: ...

    def extract_thought_signature(self, tc: Any) -> str: ...


@runtime_checkable
class StreamEventSink(Protocol):
    """流式事件接收器接口（消费事件驱动 worker 状态机，默认实现 openai_chat）"""

    id: str

    def consume(self, events: Iterable[Any], ctx: Any) -> Tuple[bool, bool]:
        """消费事件流，返回 (tool_calls_found, tool_args_pending)。

        ctx 为 worker 传入的受限状态口（暴露状态字典与 emit 回调白名单，
        不暴露 worker 实例本身）。异常语义与旧 _process_response 一致：
        截断/过滤/空响应抛 StreamInterruptedError。
        """
        ...
