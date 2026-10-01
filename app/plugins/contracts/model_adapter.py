# -*- coding: utf-8 -*-
"""模型协议适配器契约 — 把散落在 chat_worker 的协议检测分支收敛为可插拔决策。

matches(llm_config) 返回匹配优先级：0 = 不匹配；正整数越大越优先。
protocol_flags(llm_config) 返回消息序列化与 API 形态的全部协议开关，
与 message_content.messages_to_api / to_api_message 的 kwargs 一一对应。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Protocol, runtime_checkable


@dataclass
class ProtocolFlags:
    """协议行为开关（messages_to_api / to_api_message 的全部决策参数）

    serializer_id：序列化器选择（默认 "openai"）。

    protocol：端点协议形态（默认 "openai"）。决定请求发出通道与响应解析方式：
    - "openai"：chat/completions（OpenAI SDK create 调用）
    - "responses"：/v1/responses（OpenAI SDK responses.create）
    - "codeassist"：Google Code Assist 原生 REST（generateContent 形态，OAuth Bearer），
      请求体由 serializer_id 对应序列化器产出，worker 按此分派第三通道。
    默认 "openai" 保证既有 adapter 零行为变化。
    """

    is_gemini: bool = False
    requires_reasoning_content: bool = False
    use_responses_api: bool = False
    serializer_id: str = "openai"
    protocol: str = "openai"
    # adapter 透传的 provider 状态（如 Code Assist 的 gcp project id）。
    # 协议无关方不读此字段，默认空 dict 保证既有 adapter 零行为变化。
    extra: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ModelAdapter(Protocol):
    """模型协议适配器接口"""

    id: str

    def matches(self, llm_config: Dict[str, Any]) -> int:
        """返回匹配优先级（0=不匹配，越大越优先）"""
        ...

    def protocol_flags(self, llm_config: Dict[str, Any]) -> ProtocolFlags:
        """返回该 llm_config 下的协议开关"""
        ...
