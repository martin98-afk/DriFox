# -*- coding: utf-8 -*-
"""OpenAI Responses API 协议传输器 — 系统插件实现（id="openai_responses"）。

职责边界（与 openai_chat 同一层契约，2026-11-07 Phase 3 从 chat_worker 搬出）：
- 请求组装：input / instructions / tools / reasoning（effort + summary）/
  max_output_tokens / 认证头
- 请求发出：OpenAI SDK ``responses.create(stream=True)``
- 响应归一：Responses 流事件 → StreamEvent（reasoning_delta / content_delta /
  tool_call_begin / tool_args_delta / tool_call_done / usage / finish）
- 错误映射：classify_error 与 chat/completions 同口径（tool_order / missing_args）

不负责：状态机驱动（属 StreamEventSink）、重试/取消/消息缓存（属 worker）。

关键协议事实（2026-11-07 实测）：
- 思考摘要必须显式 ``reasoning.summary="auto"``，不传则服务端只给
  reasoning_tokens 计数，一个 reasoning delta 都不发。
- 工具参数的增量事件只带 ``item_id``；工具结果消息要求的 ``tool_call_id`` 是
  ``call_id``（两者不同），故本模块在流内维护 item_id → call_id 映射（局部
  变量，不落实例，遵守 ProtocolTransport 无状态纪律）。
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

from app.plugins.contracts.stream_sink import StreamEvent, StreamInterruptedError
from app.plugins.sdk import (
    build_openai_client,
    messages_to_responses_input,
    normalize_reasoning_effort,
    resolve_model_capabilities,
    tools_to_responses,
)

# 各网关的思考增量事件名（官方 + 中转兼容）
_REASONING_DELTA_EVENTS = (
    "response.reasoning_summary_text.delta",
    "response.reasoning_text.delta",
    "response.reasoning.delta",
    "response.reasoning_summary_part.delta",
)


def _ev_get(obj: Any, key: str, default: Any = None) -> Any:
    """事件/条目取值：SDK 给 pydantic 对象，部分网关给 dict。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _usage_dict(usage: Any) -> Dict[str, int]:
    """Responses usage（input/output_tokens）→ chat 口径（prompt/completion_tokens）。

    sink 的 usage 分支与缓存命中率追踪器都按 chat 口径读，这里做唯一转换点。
    """
    if not usage:
        return {}
    prompt = int(_ev_get(usage, "input_tokens", 0) or 0)
    completion = int(_ev_get(usage, "output_tokens", 0) or 0)
    total = int(_ev_get(usage, "total_tokens", 0) or 0) or (prompt + completion)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


class _EventStream:
    """StreamEvent 迭代器 + close()（worker 的 _finish_stream_response 会调 close）"""

    def __init__(self, events: Iterator[StreamEvent], response: Any) -> None:
        self._events = events
        self._response = response

    def __iter__(self) -> Iterator[StreamEvent]:
        return self._events

    def close(self) -> None:
        try:
            if self._response is not None:
                self._response.close()
        except Exception:
            pass


class OpenAIResponsesTransport:
    """Responses API 传输器（请求组装 + SDK 流 + 事件归一）"""

    id = "openai_responses"
    supports_streaming = True
    display_name = "Responses API"

    # ---------- 请求发出（ProtocolTransport 契约） ----------
    def create_stream(
        self,
        llm_config: Dict[str, Any],
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        auth_headers: Optional[Dict[str, str]] = None,
        api_messages: Optional[List[Dict[str, Any]]] = None,
        client: Any = None,
        cap_max_tokens: Any = None,
    ) -> Any:
        """序列化 → responses.create(stream=True) → 事件流。

        ``api_messages`` 是 chat 形态，本协议用不到：responses 需要
        input_items + instructions，故始终从内部格式 ``messages`` 重新序列化。
        """
        if client is None:
            client = build_openai_client(
                api_key=str(llm_config.get("API_KEY", "") or ""),
                base_url=str(llm_config.get("API_URL", "") or ""),
                timeout=120.0,
            )
        caps = resolve_model_capabilities(llm_config)
        input_items, instructions = messages_to_responses_input(
            messages, bool(caps.get("supports_vision", True))
        )
        kwargs = self.build_request_kwargs(
            llm_config, input_items, instructions, tools, auth_headers, cap_max_tokens
        )
        try:
            response = client.responses.create(**kwargs)
        except Exception as exc:
            # 第三方网关可能不认 reasoning.summary：去掉该可选参数重发一次
            # （思考仍由 effort 生效，只是没有摘要文本）
            lowered = str(exc).lower()
            reasoning = kwargs.get("reasoning") or {}
            if reasoning.get("summary") and ("summary" in lowered or "reasoning" in lowered):
                reasoning.pop("summary", None)
                response = client.responses.create(**kwargs)
            else:
                raise
        return _EventStream(self.to_events(response), response)

    def build_request_kwargs(
        self,
        llm_config: Dict[str, Any],
        input_items: List[Dict[str, Any]],
        instructions: Optional[str],
        tools: Optional[List[Dict[str, Any]]],
        auth_headers: Optional[Dict[str, str]],
        cap_max_tokens: Any = None,
    ) -> Dict[str, Any]:
        """llm_config + 序列化产物 → responses.create 参数。"""
        model = str(llm_config.get("模型名称", "") or "")
        kwargs: Dict[str, Any] = {"model": model, "input": input_items, "stream": True}
        if instructions:
            kwargs["instructions"] = instructions
        if tools:
            kwargs["tools"] = tools_to_responses(tools)
        reasoning = self._reasoning_kwargs(llm_config)
        if reasoning:
            kwargs["reasoning"] = reasoning
        max_output = llm_config.get("最大输出")
        if max_output is not None:
            kwargs["max_output_tokens"] = (
                cap_max_tokens(model, max_output) if callable(cap_max_tokens) else max_output
            )
        if auth_headers:
            kwargs["extra_headers"] = dict(auth_headers)
        return kwargs

    @staticmethod
    def _reasoning_kwargs(llm_config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """思考模式 → reasoning 参数。

        summary 必须显式给：OpenAI 语义是「不 opt in 就不返回思考摘要」，
        缺这一项时服务端只回 reasoning_tokens 计数（UI 无思考流）。
        """
        thinking = llm_config.get("思考模式")
        if thinking is True:
            caps = resolve_model_capabilities(llm_config)
            effort = normalize_reasoning_effort(
                llm_config.get("思考等级", "medium"), caps.get("reasoning_effort_values")
            )
            return {"effort": effort, "summary": "auto"}
        if thinking is False:
            return {"effort": "none"}
        return None

    # ---------- 响应归一（纯形状转换，零 worker 状态） ----------
    def to_events(self, response: Any) -> Iterator[StreamEvent]:
        """Responses 流事件 → StreamEvent。"""
        call_ids: Dict[str, str] = {}  # item_id → call_id（工具参数增量只带 item_id）
        done_ids: set = set()  # 防同一 tool_call 重复发 done
        for event in response:
            etype = str(_ev_get(event, "type", "") or "")

            if etype in _REASONING_DELTA_EVENTS:
                piece = _ev_get(event, "delta", "") or ""
                if piece:
                    yield StreamEvent(type="reasoning_delta", text=piece)
                continue

            if etype == "response.output_text.delta":
                piece = _ev_get(event, "delta", "") or ""
                if piece:
                    yield StreamEvent(type="content_delta", text=piece)
                continue

            if etype == "response.output_item.added":
                item = _ev_get(event, "item", None) or {}
                if _ev_get(item, "type") == "function_call":
                    item_id = str(_ev_get(item, "id", "") or "")
                    call_id = str(_ev_get(item, "call_id", "") or item_id)
                    name = str(_ev_get(item, "name", "") or "")
                    if item_id:
                        call_ids[item_id] = call_id
                    if name and call_id:
                        yield StreamEvent(
                            type="tool_call_begin",
                            tool_call_id=call_id,
                            name=name,
                            index=int(_ev_get(event, "output_index", -1) or -1),
                        )
                continue

            if etype == "response.function_call_arguments.delta":
                item_id = str(_ev_get(event, "item_id", "") or "")
                piece = _ev_get(event, "delta", "") or ""
                call_id = call_ids.get(item_id, item_id)
                if piece and call_id:
                    yield StreamEvent(type="tool_args_delta", tool_call_id=call_id, text=piece)
                continue

            if etype == "response.output_item.done":
                item = _ev_get(event, "item", None) or {}
                if _ev_get(item, "type") == "function_call":
                    item_id = str(_ev_get(item, "id", "") or "")
                    call_id = str(_ev_get(item, "call_id", "") or call_ids.get(item_id, item_id))
                    name = str(_ev_get(item, "name", "") or "")
                    arguments = str(_ev_get(item, "arguments", "") or "")
                    if call_id and call_id not in done_ids:
                        done_ids.add(call_id)
                        yield StreamEvent(
                            type="tool_call_done",
                            tool_call_id=call_id,
                            name=name,
                            arguments=arguments,
                        )
                continue

            if etype in ("response.completed", "response.incomplete"):
                resp = _ev_get(event, "response", None) or {}
                usage = _usage_dict(_ev_get(resp, "usage", None))
                if usage:
                    yield StreamEvent(type="usage", usage=usage)
                yield StreamEvent(
                    type="finish",
                    finish_reason="length" if etype == "response.incomplete" else "stop",
                )
                continue

            if etype in ("response.failed", "error"):
                err = _ev_get(event, "error", None)
                if err is None:
                    err = _ev_get(_ev_get(event, "response", None) or {}, "error", None)
                if isinstance(err, dict):
                    message = err.get("message", "")
                    code = err.get("code", "")
                else:
                    message = _ev_get(err, "message", "") or str(err or "")
                    code = _ev_get(err, "code", "") or ""
                raise StreamInterruptedError(f"模型响应失败: {message} ({code})")

    # ---------- 错误映射（与 chat/completions 同口径） ----------
    def classify_error(self, error_str: str) -> Optional[str]:
        lowered = (error_str or "").lower()
        if "2013" in lowered or "tool call result does not follow tool call" in lowered:
            return "tool_order"
        if "missing required arguments" in lowered or "missing a required argument" in lowered:
            return "missing_args"
        return None


def register(registry):
    """插件注册入口（transports 组件，runtime_component_loader 扫描调用）"""
    registry.register(OpenAIResponsesTransport())
