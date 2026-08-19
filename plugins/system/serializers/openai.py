# -*- coding: utf-8 -*-
"""OpenAI 默认消息序列化器 — 系统插件实现（id="openai"）。

行为零变化原则：serialize_messages 与 message_content.messages_to_api 逐点等价
（含 to_api_message 的 system/user+multimodal/assistant+tool_calls+reasoning/tool
全部分支），serialize_responses 与 messages_to_responses_input 逐点等价。
辅助函数保持 import 复用 app.core.message_content（不搬动），仅组合逻辑落在此处。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from app.core import message_content as mc
from app.plugins.contracts.message_serializer import SerializeContext


class OpenAIChatSerializer:
    """OpenAI chat/completions + Responses 形态序列化器（与旧函数逐点等价）"""

    id = "openai"

    def serialize_messages(self, messages: List[Dict[str, Any]], ctx: SerializeContext) -> List[Dict[str, Any]]:
        """内部消息列表 → chat/completions API 消息列表（等价旧 messages_to_api）"""
        api_messages: List[Dict[str, Any]] = []
        for message in messages:
            api_message = self._to_api_message(message, ctx)
            if api_message:
                if api_message.get("role") == "user" and not api_message.get("content"):
                    continue
                api_messages.append(api_message)
        return api_messages

    def serialize_responses(self, messages: List[Dict[str, Any]], ctx: SerializeContext) -> tuple:
        """内部消息列表 → (input_items, instructions)（等价旧 messages_to_responses_input）"""
        input_items: List[Dict[str, Any]] = []
        instructions_parts: List[str] = []

        for message in messages:
            normalized = mc.normalize_message(message)
            if not normalized:
                continue
            role = normalized.get("role")
            content = normalized.get("content", "")
            if role == "system":
                text = mc._extract_text_content(content)
                if text:
                    instructions_parts.append(text)
                continue
            if role == "user":
                parts = mc._extract_responses_content(content, supports_vision=ctx.supports_vision)
                if not parts:
                    continue
                input_items.append({"type": "message", "role": "user", "content": parts})
                continue
            if role == "assistant":
                tool_calls = normalized.get("tool_calls") or []
                text = mc._extract_text_content(content)
                if text:
                    input_items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    )
                for tc in tool_calls:
                    func = tc.get("function") or {}
                    call_id = tc.get("id") or tc.get("tool_call_id") or ""
                    name = func.get("name") or ""
                    arguments = func.get("arguments") or "{}"
                    if not call_id or not name:
                        continue
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": arguments
                            if isinstance(arguments, str)
                            else json.dumps(arguments, ensure_ascii=False),
                        }
                    )
                continue
            if role == "tool":
                call_id = str(normalized.get("tool_call_id", "") or "")
                if not call_id:
                    continue
                # tool 结果转为纯文本（Responses API 的 output 必须是字符串）
                output = mc._extract_text_content(content)
                if isinstance(content, list):
                    text_parts = []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            text_parts.append(str(block.get("text", "")))
                        elif block.get("type") in ("image_url", "input_image", "image"):
                            text_parts.append("[Image: base64 data]")
                    output = "\n".join(p for p in text_parts if p) or output
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": mc._prune_tool_content_for_api(str(output or "")),
                    }
                )
                continue

        instructions = "\n\n".join(p for p in instructions_parts if p)
        return input_items, instructions

    # ---------- chat/completions 单条转换（等价旧 to_api_message） ----------

    def _to_api_message(self, message: Dict[str, Any], ctx: SerializeContext) -> Dict[str, Any]:
        normalized_message = mc.normalize_message(message)
        if not normalized_message:
            return {}

        role = normalized_message.get("role")
        if role == "system":
            return {
                "role": "system",
                "content": mc._extract_text_content(normalized_message.get("content", "")),
            }
        if role == "user":
            raw_content = normalized_message.get("content", "")
            api_content = mc._extract_content_for_api(raw_content, supports_vision=ctx.supports_vision)
            api_msg = {
                "role": "user",
                "content": api_content,
            }
            # 如果 content 是 str 且有 params → 拼合上下文
            params = normalized_message.get("params", {})
            if params and isinstance(api_content, str):
                context_parts = [
                    str(value[1])
                    for value in params.values()
                    if isinstance(value, (list, tuple)) and len(value) > 1
                ]
                combined = "\n\n".join(part for part in context_parts if part)
                if combined:
                    api_msg["content"] = (
                        combined + "\n\n" + api_content
                        if api_content
                        else combined
                    )
            return api_msg
        if role == "assistant":
            api_msg: Dict[str, Any] = {
                "role": "assistant",
            }
            text = mc._extract_text_content(normalized_message.get("content", ""))
            if text:
                api_msg["content"] = text
            tool_calls = normalized_message.get("tool_calls")
            if tool_calls:
                api_msg["tool_calls"] = [
                    mc._build_api_tool_call(tc, is_gemini=ctx.flags.is_gemini) for tc in tool_calls
                ]
            # DeepSeek V4 thinking mode: 传递 reasoning_content
            reasoning = normalized_message.get("reasoning_content")
            if reasoning is not None:
                api_msg["reasoning_content"] = reasoning
            if ctx.flags.requires_reasoning_content and tool_calls and "reasoning_content" not in api_msg:
                api_msg["reasoning_content"] = ""
            # 确保 content 或 tool_calls 存在，避免 API 报 "content or tool_calls must be set"
            if "content" not in api_msg and "tool_calls" not in api_msg:
                api_msg["content"] = ""
            return api_msg
        if role == "tool":
            raw_content = normalized_message.get("content", "")
            # tool 结果支持 multimodal（如图片 base64 描述）
            tool_content = mc._extract_content_for_api(raw_content)
            # tool 消息的 content 必须是字符串（OpenAI 协议要求）
            # 如果有 image_url 块，将其转换为文本描述
            if isinstance(tool_content, list):
                tool_text_parts = []
                for block in tool_content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            tool_text_parts.append(str(block.get("text", "")))
                        elif block.get("type") == "image_url":
                            # tool 结果中的图片转为文本描述
                            tool_text_parts.append("[Image: base64 data]")
                tool_content = "\n".join(tool_text_parts)
            return {
                "role": "tool",
                "tool_call_id": str(normalized_message.get("tool_call_id", "")),
                "name": str(normalized_message.get("name", "")),
                "content": mc._prune_tool_content_for_api(str(tool_content or "")),
            }
        return {
            "role": role,
            "content": mc._extract_text_content(normalized_message.get("content", "")),
        }


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。

    source 由 loader 的 _RegistryProxy 强制为 "plugin:system"，
    本函数不显式传入，保持与 tools/providers 插件约定一致。
    """
    registry.register(OpenAIChatSerializer())
