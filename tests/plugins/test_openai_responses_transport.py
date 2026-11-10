# -*- coding: utf-8 -*-
"""openai_responses transport 单测：Responses 流事件 → StreamEvent 形状转换。

插件目录非包，按路径 importlib 加载（对齐运行时真实加载方式）。
覆盖：
1. 思考摘要 / 正文增量
2. 工具调用三段（begin 用 call_id、args 增量只带 item_id、done 带完整 arguments）
3. usage 归一为 chat 口径 + finish
4. failed 事件抛 StreamInterruptedError
5. 请求参数：reasoning.summary 必带、tools 扁平化、max_output_tokens 钳制
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.plugins.contracts.stream_sink import StreamInterruptedError

_PLUGIN = Path(__file__).resolve().parent.parent.parent / "plugins" / "system-transports"
_MODULE_PATH = _PLUGIN / "transports" / "openai_responses.py"


def _load():
    spec = importlib.util.spec_from_file_location("openai_responses_transport_test", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load()


@pytest.fixture(scope="module")
def transport(mod):
    return mod.OpenAIResponsesTransport()


def _ev(etype, **fields):
    return SimpleNamespace(type=etype, **fields)


def _item(item_type="function_call", item_id="fc_1", call_id="call_1", name="read", arguments=""):
    return SimpleNamespace(type=item_type, id=item_id, call_id=call_id, name=name, arguments=arguments)


def _types(events):
    return [e.type for e in events]


def test_reasoning_and_text_deltas(transport):
    events = list(
        transport.to_events(
            [
                _ev("response.reasoning_summary_text.delta", delta="**Plan**\n\n先看代码。"),
                _ev("response.output_text.delta", delta="结论。"),
            ]
        )
    )
    assert _types(events) == ["reasoning_delta", "content_delta"]
    assert events[0].text == "**Plan**\n\n先看代码。"
    assert events[1].text == "结论。"


def test_tool_call_three_stage(transport):
    """begin 用 call_id、参数增量按 item_id 映射回 call_id、done 带完整参数"""
    events = list(
        transport.to_events(
            [
                _ev("response.output_item.added", item=_item(), output_index=0),
                _ev("response.function_call_arguments.delta", item_id="fc_1", delta='{"path":'),
                _ev("response.function_call_arguments.delta", item_id="fc_1", delta='"a.py"}'),
                _ev("response.output_item.done", item=_item(arguments='{"path":"a.py"}')),
            ]
        )
    )
    assert _types(events) == ["tool_call_begin", "tool_args_delta", "tool_args_delta", "tool_call_done"]
    assert events[0].tool_call_id == "call_1"
    assert events[0].name == "read"
    assert events[1].tool_call_id == "call_1"
    assert events[3].arguments == '{"path":"a.py"}'


def test_completed_usage_and_finish(transport):
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, total_tokens=120)
    events = list(
        transport.to_events([_ev("response.completed", response=SimpleNamespace(usage=usage))])
    )
    assert _types(events) == ["usage", "finish"]
    assert events[0].usage == {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    assert events[1].finish_reason == "stop"


def test_incomplete_maps_to_length(transport):
    events = list(
        transport.to_events(
            [_ev("response.incomplete", response=SimpleNamespace(usage=None))]
        )
    )
    assert _types(events) == ["finish"]
    assert events[0].finish_reason == "length"


def test_failed_event_raises(transport):
    with pytest.raises(StreamInterruptedError):
        list(
            transport.to_events(
                [
                    _ev(
                        "response.failed",
                        response=SimpleNamespace(error={"message": "boom", "code": "server_error"}),
                    )
                ]
            )
        )


def test_dict_events_supported(mod, transport):
    """部分网关给 dict 事件而非 pydantic 对象"""
    events = list(transport.to_events([{"type": "response.output_text.delta", "delta": "hi"}]))
    assert _types(events) == ["content_delta"]


# ---------- 请求参数 ----------
def _cfg(**overrides):
    base = {"模型名称": "gpt-6-luna", "API_URL": "http://fake", "API_KEY": ""}
    base.update(overrides)
    return base


def test_request_kwargs_include_reasoning_summary(transport):
    kwargs = transport.build_request_kwargs(
        _cfg(思考模式=True, 思考等级="low"), [{"type": "message"}], None, None, None
    )
    assert kwargs["reasoning"]["summary"] == "auto"
    assert kwargs["reasoning"]["effort"] == "low"
    assert kwargs["stream"] is True


def test_request_kwargs_thinking_off(transport):
    kwargs = transport.build_request_kwargs(_cfg(思考模式=False), [{"type": "message"}], None, None, None)
    assert kwargs["reasoning"] == {"effort": "none"}


def test_request_kwargs_flattens_tools(transport):
    tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]
    kwargs = transport.build_request_kwargs(_cfg(), [{"type": "message"}], "sys", tools, None)
    assert kwargs["instructions"] == "sys"
    assert kwargs["tools"] == [
        {"type": "function", "name": "read", "description": "", "parameters": {"type": "object"}}
    ]


def test_request_kwargs_clamps_max_output(transport):
    kwargs = transport.build_request_kwargs(
        _cfg(最大输出=999999), [{"type": "message"}], None, None, None, cap_max_tokens=lambda m, v: 32000
    )
    assert kwargs["max_output_tokens"] == 32000


def test_summary_dropped_when_gateway_rejects(transport):
    """网关拒绝 summary → 去掉该参数重发一次（第二次请求不带 summary）"""
    calls = []

    class _FakeResponses:
        def create(self, **kwargs):
            calls.append(dict(kwargs))

            class _Err(Exception):
                pass

            if "summary" in (kwargs.get("reasoning") or {}):
                raise _Err("400 unknown parameter reasoning.summary")
            return []

    client = SimpleNamespace(responses=_FakeResponses())
    stream = transport.create_stream(
        _cfg(思考模式=True, 思考等级="low"), [{"type": "message"}], client=client
    )
    list(stream)
    assert len(calls) == 2
    assert "summary" not in calls[1]["reasoning"]
    assert calls[1]["reasoning"]["effort"] == "low"
