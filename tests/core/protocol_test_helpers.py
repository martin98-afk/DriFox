# -*- coding: utf-8 -*-
"""worker 协议通道测试辅助 — 为 mock worker 注入假 transport / sink。

背景（2026-09 协议插件化）：chat/completions 的请求组装与响应解析已从
chat_worker 下沉到 system-transports + system-stream-sinks 插件。此前测试直接
mock worker 的 `_build_api_request_kwargs` / `_process_response` / `_get_http_client`
（实现细节），迁移后这些属性不再被读取，故测试改为**经注册表注入假实现**——
测的仍是行为（重试次数、返回值语义），只是注入点从内部方法变为插件契约。

用法：
    reg = install_fake_chat_plugins(fake_client, response_result=(True, True))
    try:
        ...
    finally:
        reg.restore()
"""

from __future__ import annotations

from typing import Any, Optional, Tuple


class FakeChatTransport:
    """假 transport：create_stream 返回可空迭代的流（真实请求由 fake_client 承担）"""

    # 与真实插件同 id：注册即覆盖真实现（resolve 默认取 "openai_chat"）
    id = "openai_chat"
    supports_streaming = True

    def __init__(self, fake_client: Any) -> None:
        self._client = fake_client
        self.calls = 0

    def set_client_factory(self, factory) -> None:
        return None

    def create_stream(self, llm_config, messages, tools=None, auth_headers=None, api_messages=None):
        # 触发假 client（抛错/成功由 fake_client 决定），不真发请求
        result = self._client.chat.completions.create()
        self.calls += 1
        return iter(result if hasattr(result, "__iter__") else [])

    def build_request_kwargs(self, llm_config, cap_max_tokens=None):
        return {"model": "gpt-4o", "extra_body": {}, "_is_o1_model": False}

    def classify_error(self, error_str: str) -> Optional[str]:
        return None


class FakeStreamSink:
    """假 sink：直接返回预设 (tool_calls_found, tool_args_pending)"""

    id = "openai_chat"

    def __init__(self, result: Tuple[bool, bool] = (True, True)) -> None:
        self._result = result

    def consume(self, events, ctx) -> Tuple[bool, bool]:
        return self._result


class _RegistryGuard:
    """注册假实现并在退出时还原（防止污染其他测试）"""

    def __init__(self, transport_registry, sink_registry) -> None:
        self._transport_registry = transport_registry
        self._sink_registry = sink_registry

    def restore(self) -> None:
        self._transport_registry.unregister_source("_test_fake")
        self._sink_registry.unregister_source("_test_fake")


def install_fake_chat_plugins(fake_client: Any, response_result: Tuple[bool, bool] = (True, True)) -> _RegistryGuard:
    """向两个注册表注入以 _test_fake 为 source 的假 transport / sink。

    id 与实际插件同名（"openai_chat"）——后续注册覆盖先注册，测试注入即生效。
    """
    from app.plugins.registries.stream_sink_registry import StreamSinkRegistry
    from app.plugins.registries.transport_registry import TransportRegistry

    transport_registry = TransportRegistry.get_instance()
    sink_registry = StreamSinkRegistry.get_instance()
    transport_registry.register(FakeChatTransport(fake_client), source="_test_fake")
    sink_registry.register(FakeStreamSink(response_result), source="_test_fake")
    return _RegistryGuard(transport_registry, sink_registry)
