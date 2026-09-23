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
    from app.plugins.registries.transport_registry import TransportRegistry

    reg = TransportRegistry.get_instance()
    assert reg.resolve("not-exist") is None  # 未注册默认实现 → None


def test_stream_sink_registry_resolve_falls_back_then_none():
    from app.plugins.registries.stream_sink_registry import StreamSinkRegistry

    reg = StreamSinkRegistry.get_instance()
    assert reg.resolve("not-exist") is None


def test_transport_registry_register_and_unregister():
    from app.plugins.registries.transport_registry import TransportRegistry

    reg = TransportRegistry.get_instance()

    class Fake:
        id = "_probe_transport"

    reg.register(Fake(), source="_probe_source")
    assert reg.resolve("_probe_transport").id == "_probe_transport"
    reg.unregister_source("_probe_source")
    assert reg.resolve("_probe_transport") is None


def test_stream_sink_registry_register_and_unregister():
    from app.plugins.registries.stream_sink_registry import StreamSinkRegistry

    reg = StreamSinkRegistry.get_instance()

    class Fake:
        id = "_probe_sink"

    reg.register(Fake(), source="_probe_source")
    assert reg.resolve("_probe_sink").id == "_probe_sink"
    reg.unregister_source("_probe_source")
    assert reg.resolve("_probe_sink") is None
