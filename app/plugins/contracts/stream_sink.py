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
from typing import Any, Dict, Iterable, Protocol, Tuple, runtime_checkable

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
