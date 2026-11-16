# -*- coding: utf-8 -*-
"""Anthropic Messages 协议传输器（id="anthropic_messages"）。

适用面：任何只提供 `/v1/messages`（Anthropic 原生协议）的端点——官方
api.anthropic.com 与按模型硬分流的中转站（典型 OAIPro：gpt 系只认
chat/completions，claude 系只认 /v1/messages，打错端点报 403）。

分层（对齐 app.plugins.contracts.protocol_transport）：
- 请求组装：OpenAI 形态 api_messages → Anthropic Messages 形态（含角色合并、
  tool_calls ↔ tool_use、tool role ↔ tool_result、多模态 image_url → image）
- 请求发出：requests POST + SSE 行读
- 响应归一：SSE 事件 → StreamEvent（协议形状 → 归一化事件）
- 错误映射：Anthropic 错误串 → worker 自愈 kind（当前无匹配项，恒 None）

无状态纪律：注册表实例全进程共享，本类不持有任何 per-request 状态
（HTTP 连接随每次 create_stream 建立、随流关闭释放）。

Auth：worker 传入的 auth_headers 只管 bce 与服务商伪装头，Bearer 不在其中
（OpenAI 路径由 SDK 自己加）。因此本 transport 自己组装双头：
`x-api-key`（官方要求）+ `Authorization: Bearer`（中转站惯用）+ `anthropic-version`。
"""

from __future__ import annotations

import json
import socket
from typing import Any, Dict, Iterator, List, Optional

from app.plugins.contracts.stream_sink import StreamEvent

ANTHROPIC_VERSION = "2023-06-01"

# 默认输出上限（Anthropic 协议 max_tokens 必填）
DEFAULT_MAX_TOKENS = 8192

# 思考默认预算（开启思考但用户未给「思考预算」时）
DEFAULT_THINKING_BUDGET = 4096

# 配置键 → Anthropic 请求顶层键（只映射本协议认的参数）
_PASSTHROUGH_TOP_LEVEL = {
    "温度": "temperature",
    "top_p": "top_p",
    "top_k": "top_k",
}

# 非 API 参数（配置元数据），不得进请求体
_CONFIG_ONLY_KEYS = frozenset(
    {
        "API_KEY",
        "API_URL",
        "API_BASE",
        "认证方式",
        "模型名称",
        "系统提示",
        "启用技能",
        "协议入口",
        "name",
        "provider_name",
        "config_id",
        "display_name",
        "_suffix_index",
        "备注",
        "获取地址",
        "模型列表",
        "capability_type",
        "最大Token",
        "最大输出",
        "最大新Token",
        "思考模式",
        "思考等级",
        "思考预算",
        "cookie",
    }
)


def _messages_endpoint(api_url: str) -> str:
    """API_URL → /v1/messages 完整地址（容忍已带或不带 /v1）。

    空 API_URL 直接抛错：transport 是通用组件，不得对任何站点域名做兜底——
    静默替用户选目标 = 把他的 API Key 发给第三方。
    """
    base = str(api_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("Anthropic 通道缺少 API_URL 配置")
    if base.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


def _split_data_uri(url: str) -> Optional[Dict[str, str]]:
    """data:image/png;base64,xxxx → Anthropic image source；非 data URI 返回 None。"""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    comma = url.find(",")
    if comma == -1:
        return None
    meta = url[5:comma]
    payload = url[comma + 1 :]
    media_type = meta.split(";")[0] or "image/png"
    if "base64" not in meta:
        return None
    return {"type": "base64", "media_type": media_type, "data": payload}


def _content_blocks(content: Any, supports_vision: bool = True) -> List[Dict[str, Any]]:
    """OpenAI 形态 content → Anthropic content block 数组。"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: List[Dict[str, Any]] = []
    if not isinstance(content, list):
        return blocks
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = str(block.get("text", "") or "")
            if text:
                blocks.append({"type": "text", "text": text})
        elif btype in ("image_url", "input_image"):
            if not supports_vision:
                blocks.append({"type": "text", "text": "[Image: 已省略]"})
                continue
            holder = block.get("image_url") or block.get("image") or {}
            url = holder.get("url") if isinstance(holder, dict) else holder
            source = _split_data_uri(str(url or ""))
            if source:
                blocks.append({"type": "image", "source": source})
            elif url:
                blocks.append({"type": "image", "source": {"type": "url", "url": str(url)}})
        elif btype == "image":
            source = block.get("source")
            if isinstance(source, dict) and source.get("data"):
                blocks.append({"type": "image", "source": source})
        else:
            # 未知块降级为文本，避免静默丢内容
            text = str(block.get("text", "") or "")
            if text:
                blocks.append({"type": "text", "text": text})
    return blocks


def _tool_result_text(content: Any) -> str:
    """tool 消息 content → Anthropic tool_result 的 content 字符串。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text", "") or ""))
            elif block.get("type") in ("image_url", "input_image"):
                parts.append("[Image: base64 data]")
        return "\n".join(p for p in parts if p)
    return str(content or "")


def _parse_tool_input(arguments: Any) -> Dict[str, Any]:
    """tool_call.arguments（JSON 字符串或 dict）→ Anthropic tool_use.input。"""
    if isinstance(arguments, dict):
        return arguments
    text = str(arguments or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_anthropic_messages(
    api_messages: List[Dict[str, Any]],
    supports_vision: bool = True,
) -> tuple:
    """OpenAI 形态消息列表 → (system_prompt, anthropic_messages)。

    Anthropic 协议约束（实测 oaipro 一致）：
    - system 只能顶层给一次，多条合并为单个字符串
    - user / assistant 必须交替，连续同角色要合并 content 数组
    - 工具结果走 user 侧的 tool_result block，不是独立 role
    """
    system_parts: List[str] = []
    merged: List[Dict[str, Any]] = []

    def _push(role: str, blocks: List[Dict[str, Any]]) -> None:
        if not blocks:
            return
        if merged and merged[-1].get("role") == role:
            merged[-1]["content"].extend(blocks)
        else:
            merged.append({"role": role, "content": list(blocks)})

    for msg in api_messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "") or "")
        if role == "system":
            text = msg.get("content")
            if isinstance(text, str) and text:
                system_parts.append(text)
            elif isinstance(text, list):
                joined = "\n".join(
                    str(b.get("text", "") or "")
                    for b in text
                    if isinstance(b, dict) and b.get("type") == "text"
                )
                if joined:
                    system_parts.append(joined)
            continue
        if role == "user":
            _push("user", _content_blocks(msg.get("content", ""), supports_vision))
            continue
        if role == "assistant":
            blocks: List[Dict[str, Any]] = []
            text_blocks = _content_blocks(msg.get("content", ""), supports_vision)
            blocks.extend(text_blocks)
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                func = tc.get("function") or {}
                name = str(func.get("name", "") or "")
                if not name:
                    continue
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(tc.get("id", "") or ""),
                        "name": name,
                        "input": _parse_tool_input(func.get("arguments")),
                    }
                )
            _push("assistant", blocks)
            continue
        if role == "tool":
            call_id = str(msg.get("tool_call_id", "") or "")
            if not call_id:
                continue
            _push(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": _tool_result_text(msg.get("content", "")),
                    }
                ],
            )
            continue

    return ("\n\n".join(p for p in system_parts if p), merged)


def build_anthropic_tools(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """OpenAI function 工具列表 → Anthropic tools 形态。"""
    out: List[Dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        func = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = str(func.get("name", "") or "")
        if not name:
            continue
        item: Dict[str, Any] = {"name": name}
        desc = func.get("description")
        if desc:
            item["description"] = str(desc)
        schema = func.get("parameters")
        item["input_schema"] = schema if isinstance(schema, dict) else {"type": "object", "properties": {}}
        out.append(item)
    return out


def _map_stop_reason(stop_reason: Optional[str]) -> str:
    """Anthropic stop_reason → OpenAI finish_reason（sink 按 OpenAI 语义判截断）。"""
    mapping = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
        "max_tokens": "length",
        "refusal": "content_filter",
    }
    return mapping.get(str(stop_reason or ""), "stop")


class AnthropicMessagesStream:
    """可迭代 + 可 close() 的 SSE 流。

    - 迭代产出 StreamEvent
    - close() 释放底层连接（worker 的 _finish_stream_response 会调）
    - get_extra_info("socket")：供 worker 取消时定位 socket 做半关闭（不释放对象）
    """

    def __init__(self, resp: Any, supports_thinking: bool = True) -> None:
        self._resp = resp
        self._supports_thinking = supports_thinking
        self._closed = False
        self._events = self._iter_events()

    # ── worker 取消支持 ──
    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if name != "socket":
            return default
        try:
            conn = getattr(getattr(self._resp, "raw", None), "_connection", None)
            sock = getattr(conn, "sock", None)
            return sock if isinstance(sock, socket.socket) else default
        except Exception:
            return default

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._resp.close()
        except Exception:
            pass

    def __iter__(self) -> Iterator[StreamEvent]:
        return self._events

    # ── 事件解析 ──
    def _iter_events(self) -> Iterator[StreamEvent]:
        try:
            yield from self._parse_sse()
        finally:
            self.close()

    def _parse_sse(self) -> Iterator[StreamEvent]:
        pending_usage: Dict[str, Any] = {}
        stop_reason: Optional[str] = None
        saw_output = False
        # block index → tool_use id 映射：input_json_delta 只带 index，不带 id
        index_to_tool_id: Dict[int, str] = {}

        for raw in self._resp.iter_lines(decode_unicode=False):
            if self._closed:
                return
            if not raw:
                continue
            try:
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            except Exception:
                continue
            if not line.startswith("data:"):
                # 只消费 data 行；event: 行只是类型标签，正文里已含 type
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                if payload == "[DONE]":
                    break
                continue
            try:
                data = json.loads(payload)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            etype = str(data.get("type", "") or "")

            if etype == "message_start":
                usage = ((data.get("message") or {}).get("usage")) or {}
                pending_usage.update(_usage_from_anthropic(usage, output_key="output_tokens"))
                continue

            if etype == "content_block_start":
                block = data.get("content_block") or {}
                index = _int_or(data.get("index"), -1)
                if block.get("type") == "tool_use":
                    saw_output = True
                    tool_id = str(block.get("id", "") or "")
                    if index >= 0 and tool_id:
                        index_to_tool_id[index] = tool_id
                    yield StreamEvent(
                        type="tool_call_begin",
                        tool_call_id=tool_id,
                        name=str(block.get("name", "") or ""),
                        index=index,
                    )
                elif block.get("type") == "text" and str(block.get("text", "") or ""):
                    # 少数实现直接在 start 里给首段文本
                    saw_output = True
                    yield StreamEvent(type="content_delta", text=str(block.get("text", "")))
                continue

            if etype == "content_block_delta":
                delta = data.get("delta") or {}
                dtype = str(delta.get("type", "") or "")
                index = _int_or(data.get("index"), -1)
                if dtype == "text_delta":
                    text = str(delta.get("text", "") or "")
                    if text:
                        saw_output = True
                        yield StreamEvent(type="content_delta", text=text)
                elif dtype == "thinking_delta":
                    text = str(delta.get("thinking", "") or "")
                    if text and self._supports_thinking:
                        saw_output = True
                        yield StreamEvent(type="reasoning_delta", text=text)
                elif dtype == "input_json_delta":
                    partial = str(delta.get("partial_json", "") or "")
                    if partial:
                        saw_output = True
                        yield StreamEvent(
                            type="tool_args_delta",
                            tool_call_id=str(index_to_tool_id.get(index, "")),
                            text=partial,
                            index=index,
                        )
                continue

            if etype == "message_delta":
                delta = data.get("delta") or {}
                if delta.get("stop_reason"):
                    stop_reason = str(delta.get("stop_reason"))
                usage = data.get("usage") or {}
                pending_usage.update(_usage_from_anthropic(usage, output_key="output_tokens"))
                continue

            if etype == "error":
                err = data.get("error") or {}
                raise RuntimeError(f"服务端流式错误: {err.get('message') or data}")

            if etype == "message_stop":
                break

        if pending_usage:
            yield StreamEvent(type="usage", usage=dict(pending_usage))
        if saw_output or stop_reason:
            yield StreamEvent(type="finish", finish_reason=_map_stop_reason(stop_reason))


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _usage_from_anthropic(usage: Dict[str, Any], output_key: str = "output_tokens") -> Dict[str, Any]:
    """Anthropic usage → OpenAI 语义 usage（sink 与 cache_tracker 都按 OpenAI 形态读）。

    换算依据（实测 oaipro 回包）：Anthropic 的 input_tokens 不含缓存命中
    （cache_read_input_tokens 与之并列），而 OpenAI 的 prompt_tokens 含缓存。
    故 prompt_tokens = input + cache_read + cache_creation；命中量放
    prompt_tokens_details.cached_tokens，cache_tracker 据此算命中率，
    上下文占用也才能拿到真实总输入量。
    """
    if not isinstance(usage, dict):
        return {}
    base_input = _int_or(usage.get("input_tokens"), 0)
    cache_read = _int_or(usage.get("cache_read_input_tokens"), 0)
    cache_creation = _int_or(usage.get("cache_creation_input_tokens"), 0)
    cc = usage.get("cache_creation")
    if isinstance(cc, dict):
        cache_creation = max(cache_creation, sum(_int_or(v, 0) for v in cc.values()))
    output = _int_or(usage.get(output_key), 0)
    prompt = base_input + cache_read + cache_creation
    return {
        "prompt_tokens": prompt,
        "completion_tokens": output,
        "total_tokens": prompt + output,
        "prompt_tokens_details": {"cached_tokens": cache_read},
    }


class AnthropicMessagesTransport:
    """Anthropic Messages 协议传输器（无状态，进程级共享实例）。"""

    id = "anthropic_messages"
    supports_streaming = True

    # ---------- 组装 ----------

    def build_request(self, llm_config: Dict[str, Any], cap_max_tokens: Any = None) -> Dict[str, Any]:
        """llm_config → Anthropic 请求体（不含 messages/system/tools）。"""
        cfg = llm_config or {}
        model = str(cfg.get("模型名称", "") or "").strip()
        requested = _int_or(cfg.get("最大输出") or cfg.get("最大Token") or 0, 0)
        if callable(cap_max_tokens) and model:
            try:
                requested = _int_or(cap_max_tokens(model, requested), requested)
            except Exception:
                pass
        max_tokens = requested if requested > 0 else DEFAULT_MAX_TOKENS

        body: Dict[str, Any] = {"model": model, "max_tokens": max_tokens}

        for cn_key, en_key in _PASSTHROUGH_TOP_LEVEL.items():
            value = cfg.get(cn_key)
            if value is None or value == "":
                continue
            body[en_key] = value

        # 思考：Anthropic 用 thinking.budget_tokens，且 max_tokens 必须大于预算
        if cfg.get("思考模式") is True:
            budget = _int_or(cfg.get("思考预算") or DEFAULT_THINKING_BUDGET, DEFAULT_THINKING_BUDGET)
            budget = max(1024, budget)
            if max_tokens <= budget:
                max_tokens = budget + 1024
                body["max_tokens"] = max_tokens
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # 开启 thinking 时 Anthropic 只接受 temperature=1
            body.pop("temperature", None)
        return body

    # ---------- 请求发出 ----------

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
        """发请求并返回产出 StreamEvent 的流（惰性迭代，调用即发请求）。

        client 入参（worker 的 OpenAI 客户端）本协议用不上——Anthropic 形态
        与 OpenAI SDK 的请求体不兼容，这里自建 requests 连接。
        """
        import requests

        cfg = llm_config or {}
        model = str(cfg.get("模型名称", "") or "").strip()
        if not model:
            raise RuntimeError("Anthropic 通道缺少「模型名称」配置")
        # 端点校验前置：配置缺失要报「缺什么」，而不是被消息转换的错误掩盖
        url = _messages_endpoint(str(cfg.get("API_URL", "") or ""))

        source = api_messages if api_messages is not None else messages
        supports_vision = bool(cfg.get("supports_vision", True))
        system_prompt, anth_messages = build_anthropic_messages(source, supports_vision)
        if not anth_messages:
            raise RuntimeError("Anthropic 通道没有可发送的消息")

        body = self.build_request(cfg, cap_max_tokens=cap_max_tokens)
        body["messages"] = anth_messages
        if system_prompt:
            body["system"] = system_prompt
        anth_tools = build_anthropic_tools(tools)
        if anth_tools:
            body["tools"] = anth_tools
        body["stream"] = True

        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        api_key = str(cfg.get("API_KEY", "") or "").strip()
        if api_key:
            # 双头兼容：Anthropic 官方只认 x-api-key，多数中转站（含 oaipro 实测）
            # 认 Bearer；两个都带，服务端各取所需，互不干扰
            headers["x-api-key"] = api_key
            headers["authorization"] = f"Bearer {api_key}"
        if auth_headers:
            # worker 算好的伪装头（extra_headers）优先，但不得覆盖协议必需头
            for key, value in auth_headers.items():
                if str(key).lower() in ("content-type", "accept", "anthropic-version"):
                    continue
                headers[key] = value

        resp = None
        last_error: Optional[Exception] = None
        # 首次连接偶发 ProxyError/RemoteDisconnected（本机代理到站点的首连不稳），
        # 重试一次即可，不叠加到 worker 的重试链（那会整轮重发消息）
        for attempt in range(2):
            try:
                resp = requests.post(
                    url,
                    headers=headers,
                    json=body,
                    stream=True,
                    timeout=(30, 600),
                )
                break
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    import time as _time

                    _time.sleep(1.0)
        if resp is None:
            raise RuntimeError(
                f"Anthropic 通道请求失败: {type(last_error).__name__}: {last_error}"
            ) from last_error

        if resp.status_code != 200:
            detail = ""
            try:
                detail = (resp.text or "")[:500]
            except Exception:
                pass
            status = resp.status_code
            try:
                resp.close()
            except Exception:
                pass
            raise RuntimeError(f"Anthropic 通道 HTTP {status}: {detail}")

        return AnthropicMessagesStream(resp, supports_thinking=cfg.get("思考模式") is True)

    # ---------- 错误映射 ----------

    def classify_error(self, error_str: str) -> Optional[str]:
        """当前无 Anthropic 专属自愈动作，恒 None（worker 走通用重试/报错链）。"""
        return None


def register(registry):
    registry.register(AnthropicMessagesTransport())
