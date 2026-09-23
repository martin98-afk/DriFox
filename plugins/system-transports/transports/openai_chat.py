# -*- coding: utf-8 -*-
"""OpenAI chat/completions 协议传输器 — 系统插件实现（id="openai_chat"）。

职责边界（两层契约的第一层）：
- 请求组装：从 chat_worker._build_api_request_kwargs 原地搬迁（extra_body / 思考参数
  映射 / bce 认证 / 服务商伪装头 / max_tokens 钳制），行为逐点等价
- 请求发出：OpenAI SDK chat.completions.create（httpx 客户端由 worker 注入）
- 响应归一：SDK chunk → StreamEvent（纯形状转换，零 worker 状态）
- 错误映射：classify_error 把「2013 tool result order」「Missing required arguments」
  映射为 heal kind，修复动作由 worker 执行（transport 不持有消息缓存）

不负责：状态机驱动（属 StreamEventSink）、重试/取消（属 worker）。
"""

from __future__ import annotations

import base64
import re
from typing import Any, Dict, Iterator, List, Optional

from app.constants import PARAM_SCHEMA
from app.constants import provider_quota_exclude_keys as QUOTA_EXCLUDE_KEYS
from app.core.modelmeta.model_capabilities import get_model_capabilities, normalize_reasoning_effort
from app.core.modelmeta.provider_profile import get_provider_profile
from app.plugins.contracts.stream_sink import StreamEvent

_VALID_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# 组装 extra_body 时排除的配置键（非 API 参数）
_CONFIG_ONLY_KEYS = {
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
}


class OpenAIChatTransport:
    """chat/completions 传输器（请求组装 + SDK 流 + 事件归一）"""

    id = "openai_chat"
    supports_streaming = True

    def __init__(self, client_factory=None) -> None:
        """client_factory：无参回调，返回复用的 OpenAI SDK 客户端（每次请求调用）。

        worker 在构造 transport 实例后注入（`set_client_factory`），
        使 worker 保持单一 httpx 连接池与既有超时配置；未注入时按 llm_config 自建
        （插件独立使用场景）。
        """
        self._client_factory = client_factory

    def set_client_factory(self, factory) -> None:
        """worker 注入客户端工厂（幂等覆盖）"""
        self._client_factory = factory

    def _get_client(self, llm_config: Dict[str, Any]):
        if self._client_factory is not None:
            return self._client_factory()
        from app.utils.http_client import build_openai_client

        return build_openai_client(
            api_key=str(llm_config.get("API_KEY", "") or ""),
            base_url=llm_config.get("API_URL"),
            timeout=600.0,
        )

    # ---------- 请求组装（纯函数，便于单测） ----------

    def build_request_kwargs(self, llm_config: Dict[str, Any], cap_max_tokens=None) -> Dict[str, Any]:
        """llm_config → (model, stream, extra_body, auth_headers, is_o1)。

        与 chat_worker._build_api_request_kwargs 逐点等价（含思考模式三分支、
        bce Basic 认证、服务商伪装头合并）；`cap_max_tokens` 为 worker 注入的
        token 上限钳制函数（签名 (model, requested) -> int），缺省不做钳制。
        """
        model = str(llm_config.get("模型名称", "gpt-4o") or "gpt-4o")
        extra_body: Dict[str, Any] = {}

        skip_params = {"temperature", "top_p", "presence_penalty", "frequency_penalty"}
        is_o1 = model.startswith("o1") or model.startswith("o3")
        if is_o1:
            skip_params.update({"temperature", "top_p"})

        for cn_key, value in llm_config.items():
            if cn_key in _CONFIG_ONLY_KEYS or cn_key in QUOTA_EXCLUDE_KEYS():
                continue
            meta = PARAM_SCHEMA.get(cn_key, {})
            en_key = meta.get("api_param")
            if not en_key and _VALID_IDENTIFIER_PATTERN.match(cn_key):
                en_key = cn_key
            if not en_key or en_key in skip_params or en_key == "max_tokens":
                continue
            extra_body[en_key] = value

        max_tokens = llm_config.get("最大Token")
        if max_tokens is not None:
            extra_body["max_tokens"] = cap_max_tokens(model, max_tokens) if callable(cap_max_tokens) else max_tokens

        self._apply_thinking(extra_body, llm_config, model)

        auth_headers = None
        if str(llm_config.get("认证方式", "bearer") or "bearer") == "bce":
            api_key = str(llm_config.get("API_KEY", "") or "")
            b64_auth = base64.b64encode(f"{api_key}:{api_key}".encode()).decode()
            auth_headers = {"Authorization": f"Basic {b64_auth}"}
        return {
            "model": model,
            "extra_body": extra_body,
            "_auth_headers": auth_headers,
            "_is_o1_model": is_o1,
        }

    @staticmethod
    def _apply_thinking(extra_body: Dict[str, Any], llm_config: Dict[str, Any], model: str) -> None:
        """思考模式映射（通用逻辑，不按 family 硬编码）——等价搬迁自 worker"""
        thinking_mode = llm_config.get("思考模式")
        if thinking_mode is None:
            return
        caps = get_model_capabilities(model) or {}
        t_param = caps.get("thinking_param")
        enable_value = caps.get("thinking_enable_value", "enabled")
        if not t_param:
            t_param = get_provider_profile(llm_config).get("thinking_param")

        if thinking_mode is True:
            if t_param == "thinking":
                extra_body["thinking"] = {"type": enable_value}
                extra_body.pop("reasoning_effort", None)
                extra_body.pop("thinking_budget", None)
            elif t_param == "thinking_budget":
                extra_body["thinking_budget"] = llm_config.get("思考预算", 4096)
                extra_body.pop("reasoning_effort", None)
                extra_body.pop("thinking", None)
            elif t_param == "reasoning_effort":
                if "reasoning_effort" not in extra_body:
                    extra_body["reasoning_effort"] = normalize_reasoning_effort(
                        llm_config.get("思考等级", "medium"), caps.get("reasoning_effort_values")
                    )
                extra_body.pop("thinking", None)
                extra_body.pop("thinking_budget", None)
        else:  # False - 关闭思考
            extra_body["thinking"] = {"type": "disabled"}
            extra_body.pop("thinking_budget", None)
            extra_body.pop("reasoning_effort", None)

    # ---------- 请求发出（ProtocolTransport 契约） ----------

    def create_stream(
        self,
        llm_config: Dict[str, Any],
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        auth_headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        """返回产出 StreamEvent 的迭代器（惰性：调用时立即发请求，迭代时拉取 chunk）。"""
        client = self._get_client(llm_config)
        kwargs = self.build_request_kwargs(llm_config)
        req_kwargs: Dict[str, Any] = {
            "model": kwargs["model"],
            "messages": messages,
            "stream": bool(self.supports_streaming),
        }
        if kwargs["extra_body"]:
            req_kwargs["extra_body"] = kwargs["extra_body"]
        merged_headers = {**(kwargs["_auth_headers"] or {}), **(auth_headers or {})}
        if merged_headers:
            req_kwargs["extra_headers"] = merged_headers
        if tools:
            req_kwargs["tools"] = tools
        response = client.chat.completions.create(**req_kwargs)
        return _EventStream(self.to_events(response), response)

    # ---------- 响应归一（纯形状转换，零 worker 状态） ----------

    def to_events(self, chunks) -> Iterator[StreamEvent]:
        """OpenAI SDK chunk 流 → StreamEvent 流。

        覆盖形状（每种都有对应单测）：
        - 空 choices + usage（部分模型 usage 在独立 chunk）
        - delta.content → content_delta
        - delta.reasoning_content → reasoning_delta
        - delta.tool_calls：首 chunk 带 id+name，后续 chunk 仅 index+arguments
        - thought_signature 透传（Gemini 多轮必需）
        - finish_reason → finish（None 不发）
        """
        for chunk in chunks:
            choices = getattr(chunk, "choices", None) or []
            usage = getattr(chunk, "usage", None)
            if not choices:
                if usage:
                    yield self._usage_event(usage)
                continue

            choice = choices[0]
            delta = getattr(choice, "delta", None)
            finish_reason = getattr(choice, "finish_reason", None)

            if delta is not None:
                content = getattr(delta, "content", None)
                if content:
                    yield StreamEvent(type="content_delta", text=content)

                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    yield StreamEvent(type="reasoning_delta", text=reasoning)

                for tc in getattr(delta, "tool_calls", None) or []:
                    tc_id = getattr(tc, "id", "") or ""
                    func = getattr(tc, "function", None)
                    name = (getattr(func, "name", "") if func is not None else "") or ""
                    args = (getattr(func, "arguments", "") if func is not None else "") or ""
                    signature = getattr(tc, "thought_signature", "") or ""
                    if name:
                        # 首 chunk：带 id + name（args 可能为空）
                        yield StreamEvent(
                            type="tool_call_begin",
                            tool_call_id=tc_id,
                            name=name,
                            thought_signature=signature,
                        )
                    if args:
                        yield StreamEvent(
                            type="tool_args_delta",
                            tool_call_id=tc_id,
                            name=name,
                            text=args,
                        )

            if usage:
                yield self._usage_event(usage)
            if finish_reason:
                yield StreamEvent(type="finish", finish_reason=str(finish_reason))

    @staticmethod
    def _usage_event(usage: Any) -> StreamEvent:
        return StreamEvent(
            type="usage",
            usage={
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(usage, "total_tokens", 0) or 0,
            },
        )

    # ---------- 错误映射（自愈 kind，修复动作由 worker 执行） ----------

    @staticmethod
    def classify_error(error_str: str) -> Optional[str]:
        """OpenAI 错误串 → heal kind（None = 无需自愈）"""
        lowered = error_str.lower()
        if "2013" in error_str or "tool call result does not follow tool call" in lowered:
            return "tool_order"
        if "missing required arguments" in lowered or "missing a required argument" in lowered:
            return "missing_args"
        return None


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


def register(registry):
    """插件注册入口（transports 组件，runtime_component_loader 扫描调用）"""
    registry.register(OpenAIChatTransport())
