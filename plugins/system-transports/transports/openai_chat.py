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

import re
from typing import Any, Dict, Iterator, List, Optional

from app.plugins.contracts.stream_sink import StreamEvent
from app.plugins.sdk import (
    PARAM_SCHEMA,
    get_model_capabilities,
    get_provider_profile,
    normalize_reasoning_effort,
    provider_quota_exclude_keys as QUOTA_EXCLUDE_KEYS,
    build_openai_client,
)

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
    """chat/completions 传输器（请求组装 + SDK 流 + 事件归一）

    无状态实现：注册表返回的实例被全进程共享，per-request 资源（HTTP 客户端 /
    token 钳制函数）一律经 create_stream 入参传递，不写实例属性——写入即被其他
    worker 覆盖，导致请求打到别家服务商端点（模型名与端点错配 400）。
    """

    id = "openai_chat"
    # 默认支持流式；模型级例外由 supports_streaming_for 按 llm_config 判定
    supports_streaming = True

    def supports_streaming_for(self, llm_config: Dict[str, Any]) -> bool:
        """模型级流式能力：o1/o3 系列（非 gpt-5+）的 chat/completions 不接受 stream 参数。

        worker 经 hasattr 探测本方法（可选能力），未实现时用类属性 supports_streaming。
        """
        model = str((llm_config or {}).get("模型名称", "") or "")
        if model.startswith("o1") or model.startswith("o3"):
            return False
        return True

    def __init__(self, client_factory=None, cap_max_tokens=None) -> None:
        """构造期注入（可选）：供插件独立使用场景（不依赖 worker）预先绑定资源。

        ⚠️ 注册表返回的实例全进程共享，worker 路径**不得**走这里——它已改为经
        create_stream(client=..., cap_max_tokens=...) 逐请求传入，与实例无关。
        本入参只在插件自己 new 一个 transport 自用时使用（如单测/脚本）。
        """
        self._client_factory = client_factory
        self._cap_max_tokens = cap_max_tokens

    def set_client_factory(self, factory) -> None:
        """兼容接口：旧版 worker 经 hasattr 探测后调用（写入实例属性）。

        ⚠️ **不可删除**：插件热重载即刻生效，主程序需重启。旧主程序 + 新插件是
        常态组合，删掉本方法会让旧 worker 的 hasattr 探测落空 → 不注入 →
        连接池/钳制双双失效（实测直接 400 code 1210）。新版 worker 不再调用本方法。

        副作用提醒：直接写共享实例属性，多 worker 并发会互相覆盖（模型名与端点
        错配 400 的根因）——旧 worker 路径由 worker 侧浅拷贝隔离兜底。
        """
        self._client_factory = factory

    def set_cap_max_tokens(self, cap) -> None:
        """兼容接口：旧版 worker 注入 token 上限钳制函数。同 set_client_factory，
        **不可删除**（旧主程序依赖 hasattr 探测到它才会注入，缺失即 400 code 1210）。"""
        self._cap_max_tokens = cap

    def _get_client(self, llm_config: Dict[str, Any]):
        if self._client_factory is not None:
            return self._client_factory()

        return build_openai_client(
            api_key=str(llm_config.get("API_KEY", "") or ""),
            base_url=llm_config.get("API_URL"),
            timeout=600.0,
        )

    # ---------- 请求组装（纯函数，便于单测） ----------

    def build_request_kwargs(self, llm_config: Dict[str, Any], cap_max_tokens=None) -> Dict[str, Any]:
        """llm_config → (model, stream, extra_body, auth_headers, is_o1)。

        与 chat_worker._build_api_request_kwargs 逐点等价（含思考模式三分支、
        bce Basic 认证、服务商伪装头合并）；`cap_max_tokens` 为 per-request 传入的
        token 上限钳制函数（签名 (model, requested) -> int），缺省不做钳制。
        """
        if cap_max_tokens is None:
            cap_max_tokens = self._cap_max_tokens
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

        # 认证头由 worker 统一组装（_build_auth_headers：bce + 服务商伪装头）后经
        # create_stream(auth_headers=...) 注入——transport 不重复实现认证逻辑，
        # 保持「协议形状转换」单一职责。
        return {
            "model": model,
            "extra_body": extra_body,
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
        api_messages: Optional[List[Dict[str, Any]]] = None,
        client: Any = None,
        cap_max_tokens: Any = None,
    ) -> Any:
        """返回产出 StreamEvent 的迭代器（惰性：调用时立即发请求，迭代时拉取 chunk）。

        api_messages：worker 已完成序列化（且可能经自愈修正）的 API 格式消息。
        传入时直接使用（跳过内部序列化），避免与 worker 的 _api_messages_cache /
        自愈修正结果不一致；为 None 时按 messages 自行序列化（独立使用场景）。

        client / cap_max_tokens：per-request 资源（worker 逐请求传入）。为 None 时
        回退构造期注入值（插件独立使用场景），再回退默认行为（自建客户端 / 不钳制）。
        """
        if client is None:
            client = self._get_client(llm_config)
        kwargs = self.build_request_kwargs(llm_config, cap_max_tokens=cap_max_tokens)
        if api_messages is None:
            api_messages = messages
        req_kwargs: Dict[str, Any] = {
            "model": kwargs["model"],
            "messages": api_messages,
            # 模型级流式判定（o1/o3 不接受 stream）；worker 也会同步 stream=False
            "stream": self.supports_streaming_for(llm_config),
        }
        if kwargs["extra_body"]:
            req_kwargs["extra_body"] = kwargs["extra_body"]
        # 认证头全部来自 worker 注入（唯一组装点）；独立使用时无认证头
        if auth_headers:
            req_kwargs["extra_headers"] = dict(auth_headers)
        if tools:
            req_kwargs["tools"] = tools
        response = client.chat.completions.create(**req_kwargs)
        return _EventStream(self.to_events(response), response)

    # ---------- 响应归一（纯形状转换，零 worker 状态） ----------

    def to_events(self, chunks) -> Iterator[StreamEvent]:
        """OpenAI SDK 响应 → StreamEvent 流（chunk 流 / 非流式 completion 二选一）。

        流式（Stream 迭代器）覆盖形状（每种都有对应单测）：
        - 空 choices + usage（部分模型 usage 在独立 chunk）
        - delta.content → content_delta
        - delta.reasoning_content → reasoning_delta
        - delta.tool_calls：首 chunk 带 id+name，后续 chunk 仅 index+arguments
        - thought_signature 透传（Gemini 多轮必需）
        - finish_reason → finish（None 不发）

        非流式（stream=False，o1/o3 系列）：ChatCompletion 单对象，归一见
        _completion_to_events。判据：响应对象自身携带 list 型 choices
        （chunk 流迭代器无该属性，不会被误判）。
        """
        if isinstance(getattr(chunks, "choices", None), list):
            yield from self._completion_to_events(chunks)
            return
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
                    tc_index = getattr(tc, "index", None)
                    index = int(tc_index) if isinstance(tc_index, int) else -1
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
                            index=index,
                        )
                    if args:
                        yield StreamEvent(
                            type="tool_args_delta",
                            tool_call_id=tc_id,
                            name=name,
                            text=args,
                            index=index,
                        )

            if usage:
                yield self._usage_event(usage)
            if finish_reason:
                yield StreamEvent(type="finish", finish_reason=str(finish_reason))

    @classmethod
    def _completion_to_events(cls, response: Any) -> Iterator[StreamEvent]:
        """stream=False 的 ChatCompletion 单对象 → StreamEvent（o1/o3 等非流式通道）。

        与流式路径同构：tool_call 归一为 begin+args_delta 对（sink 按 begin 建
        buffer、args 走既有 JSON 解析路径）。usage / finish 不在此发：
        - usage：sink 对非流式（ctx.stream=False）统一从 response 对象记账，
          重复发会双份 record_usage；
        - finish：非流式无 SSE 截断概念，缺席时 sink 截断检测语义不变。
        """
        choices = getattr(response, "choices", None) or []
        if not choices:
            return
        message = getattr(choices[0], "message", None)
        if message is None:
            return
        reasoning = getattr(message, "reasoning_content", None)
        if reasoning:
            # 思考先于正文 emit，保持 UI 思考块在前
            yield StreamEvent(type="reasoning_delta", text=reasoning)
        content = getattr(message, "content", None)
        if content:
            yield StreamEvent(type="content_delta", text=content)
        for tc in getattr(message, "tool_calls", None) or []:
            func = getattr(tc, "function", None)
            name = (getattr(func, "name", "") if func is not None else "") or ""
            if not name:
                # 无名的完整调用无法建 buffer（与流式孤立 delta 同规则），跳过
                continue
            args = (getattr(func, "arguments", "") if func is not None else "") or ""
            tc_index = getattr(tc, "index", None)
            index = int(tc_index) if isinstance(tc_index, int) else -1
            tc_id = (getattr(tc, "id", "") or "") or (f"index_{index}" if index != -1 else "completion_0")
            signature = getattr(tc, "thought_signature", "") or ""
            yield StreamEvent(
                type="tool_call_begin",
                tool_call_id=tc_id,
                name=name,
                thought_signature=signature,
                index=index,
            )
            if args:
                yield StreamEvent(
                    type="tool_args_delta",
                    tool_call_id=tc_id,
                    name=name,
                    text=args,
                    index=index,
                )

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
