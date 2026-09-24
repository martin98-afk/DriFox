# -*- coding: utf-8 -*-
"""StreamEvent / StreamEventSink 契约与两个新注册表的回归测试。"""

import pytest


def test_event_types_are_closed_set():
    from app.plugins.contracts.stream_sink import EVENT_TYPES

    assert EVENT_TYPES == {
        "content_delta",
        "reasoning_delta",
        "tool_call_begin",
        "tool_args_delta",
        "tool_call_done",
        "usage",
        "finish",
    }


def test_event_rejects_unknown_type():
    from app.plugins.contracts.stream_sink import StreamEvent

    with pytest.raises(ValueError, match="未知流式事件类型"):
        StreamEvent(type="not_a_type")


def test_event_defaults():
    from app.plugins.contracts.stream_sink import StreamEvent

    ev = StreamEvent(type="content_delta", text="hi")
    assert ev.tool_call_id == "" and ev.usage == {} and ev.thought_signature == ""


def test_sink_protocol_shape():
    from app.plugins.contracts.stream_sink import StreamEventSink

    class Fake:
        id = "x"

        def consume(self, events, ctx):
            return False, False

    assert isinstance(Fake(), StreamEventSink)
    assert not isinstance(object(), StreamEventSink)


def test_transport_protocol_includes_new_capabilities():
    from app.plugins.contracts.protocol_transport import ProtocolTransport

    class Fake:
        id = "t"
        supports_streaming = True

        def create_stream(self, llm_config, messages, tools=None, auth_headers=None):
            return iter(())

        def classify_error(self, error_str):
            return None

    assert isinstance(Fake(), ProtocolTransport)


def test_transport_registry_resolve_falls_back_then_none():
    """独立实例（不依赖全局单例状态）：无任何注册时 resolve 返回 None"""
    from app.plugins.registries.transport_registry import TransportRegistry

    reg = TransportRegistry()
    assert reg.resolve("not-exist") is None


def test_stream_sink_registry_resolve_falls_back_then_none():
    from app.plugins.registries.stream_sink_registry import StreamSinkRegistry

    reg = StreamSinkRegistry()
    assert reg.resolve("not-exist") is None


def test_transport_registry_register_and_unregister():
    from app.plugins.registries.transport_registry import TransportRegistry

    reg = TransportRegistry()

    class Fake:
        id = "_probe_transport"

    reg.register(Fake(), source="_probe_source")
    assert reg.resolve("_probe_transport").id == "_probe_transport"
    reg.unregister_source("_probe_source")
    assert reg.resolve("_probe_transport") is None


def test_stream_sink_registry_register_and_unregister():
    from app.plugins.registries.stream_sink_registry import StreamSinkRegistry

    reg = StreamSinkRegistry()

    class Fake:
        id = "_probe_sink"

    reg.register(Fake(), source="_probe_source")
    assert reg.resolve("_probe_sink").id == "_probe_sink"
    reg.unregister_source("_probe_source")
    assert reg.resolve("_probe_sink") is None

# ---------- transport 无状态纪律（per-request 注入分流） ----------


def test_create_stream_accepts_per_request_resources():
    """契约：create_stream 接受 client / cap_max_tokens 逐请求入参。

    不接受即回退成写共享单例属性，并发 worker 会互相覆盖，
    表现为模型名与端点错配的 400。
    """
    import inspect

    from app.plugins.contracts.protocol_transport import ProtocolTransport

    params = inspect.signature(ProtocolTransport.create_stream).parameters
    assert "client" in params
    assert "cap_max_tokens" in params


def test_worker_signature_probe_detects_new_and_old_impls():
    """worker 签名探测：新实现走首选路径，仅有 set_* 的旧实现走浅拷贝兼容路径。

    探测用签名而非 hasattr：兼容期实现可能同时保留 set_* 方法，
    hasattr 无法区分「支持新入参」与「只支持旧注入」。
    """
    from app.core.workers.chat_worker import _accepts_per_request_client

    class NewImpl:
        def create_stream(self, llm_config, messages, tools=None, auth_headers=None, api_messages=None, client=None, cap_max_tokens=None):
            return iter(())

    class OldImpl:
        def create_stream(self, llm_config, messages, tools=None, auth_headers=None, api_messages=None):
            return iter(())

        def set_client_factory(self, factory) -> None:
            return None

        def set_cap_max_tokens(self, cap) -> None:
            return None

    class KwargsImpl:
        def create_stream(self, llm_config, messages, **kwargs):
            return iter(())

    assert _accepts_per_request_client(NewImpl().create_stream) is True
    assert _accepts_per_request_client(OldImpl().create_stream) is False
    # **kwargs 形式视为支持（无法否定时不误判）
    assert _accepts_per_request_client(KwargsImpl().create_stream) is True
