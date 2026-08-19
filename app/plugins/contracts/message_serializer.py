# -*- coding: utf-8 -*-
"""消息序列化器契约 — 把 message_content 的协议特判序列化逻辑收敛为可插拔组件。

与 ModelAdapter（决策层）对称：adapter 决定「协议开关」，serializer 决定「消息形态」。
Phase B 只交付契约 + 默认实现（openai 与旧逻辑逐点等价），路由决策（chat vs responses）
保持 worker 现状不变，序列化器提供两个独立方法分别等价旧函数。

- serialize_messages  ≡ 旧 messages_to_api（返回 List[Dict]）
- serialize_responses ≡ 旧 messages_to_responses_input（返回 (input_items, instructions) tuple）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol, runtime_checkable

from app.plugins.contracts.model_adapter import ProtocolFlags


@dataclass
class SerializeContext:
    """一次序列化调用的上下文（能力 + 协议开关）。

    supports_vision：模型是否支持视觉（来自 provider 元数据查询，由 worker 注入，
    不纳入 ProtocolFlags——那是协议特判而非模型能力）。
    flags：由 ModelAdapter 解析的协议开关（is_gemini / requires_reasoning_content /
    use_responses_api / serializer_id）。
    """

    supports_vision: bool = True
    flags: ProtocolFlags = field(default_factory=ProtocolFlags)


@dataclass
class SerializeResult:
    """单入口序列化结果（Phase C：worker 从「按形态调 3 个函数」收敛为 1 个入口）。

    序列化器内部按 ctx.flags.use_responses_api 路由到对应形态：
    - chat/completions 形态 → messages（等价旧 messages_to_api）
    - responses 形态 → input_items + instructions（等价旧 messages_to_responses_input）
    未使用的形态字段保持空默认值。
    """

    messages: List[Dict[str, Any]] = field(default_factory=list)
    input_items: List[Dict[str, Any]] = field(default_factory=list)
    instructions: str = ""


@runtime_checkable
class MessageSerializer(Protocol):
    """消息序列化器接口（id 唯一，注册表按 id 解析，回退 "openai"）"""

    id: str

    def serialize(self, messages: List[Dict[str, Any]], ctx: SerializeContext) -> SerializeResult:
        """单入口：按 ctx.flags.use_responses_api 路由到 chat/responses 形态（Phase C）"""
        ...

    def serialize_messages(self, messages: List[Dict[str, Any]], ctx: SerializeContext) -> List[Dict[str, Any]]:
        """内部消息列表 → chat/completions API 消息列表（等价旧 messages_to_api）"""
        ...

    def serialize_responses(self, messages: List[Dict[str, Any]], ctx: SerializeContext) -> tuple:
        """内部消息列表 → (input_items, instructions)（等价旧 messages_to_responses_input）"""
        ...
