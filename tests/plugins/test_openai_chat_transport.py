# -*- coding: utf-8 -*-
"""openai_chat transport 单测：SDK chunk → StreamEvent 形状转换（纯函数，零 worker 状态）。

插件目录非包，按路径 importlib 加载（对齐运行时真实加载方式）。
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_PLUGIN = Path(__file__).resolve().parent.parent.parent / "plugins" / "system-transports"
_MODULE_PATH = _PLUGIN / "transports" / "openai_chat.py"


def _load():
    spec = importlib.util.spec_from_file_location("openai_chat_transport_test", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load()


@pytest.fixture(scope="module")
def transport(mod):
    return mod.OpenAIChatTransport()


def _chunk(content=None, tool_calls=None, finish_reason=None, reasoning=None, usage=None, choices_empty=False):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=reasoning)
    if choices_empty:
        return SimpleNamespace(choices=[], usage=usage)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason, index=0)],
        usage=usage,
    )


def _tc(index=0, tc_id=None, name=None, arguments=None, thought_signature=None):
    func = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=tc_id, function=func, thought_signature=thought_signature)


# ---------- 1. 纯文本 + finish ----------


def test_text_delta_and_finish(transport):
    events = list(transport.to_events([_chunk(content="你好"), _chunk(finish_reason="stop")]))
    assert [(e.type, e.text) for e in events if e.type == "content_delta"] == [("content_delta", "你好")]
    assert events[-1].type == "finish" and events[-1].finish_reason == "stop"


# ---------- 2. 空 choices 仍读 usage ----------


def test_empty_choices_still_emits_usage(transport):
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    events = list(transport.to_events([_chunk(choices_empty=True, usage=usage)]))
    assert len(events) == 1 and events[0].type == "usage"
    assert events[0].usage == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}


# ---------- 3. 工具调用：首 chunk 带 id+name，后续仅 index+arguments ----------


def test_tool_call_begin_then_args_delta(transport):
    events = list(
        transport.to_events(
            [
                _chunk(tool_calls=[_tc(index=0, tc_id="call_1", name="run", arguments="")]),
                _chunk(tool_calls=[_tc(index=0, arguments='{"a"')]),
                _chunk(tool_calls=[_tc(index=0, arguments=":1}")]),
            ]
        )
    )
    begins = [e for e in events if e.type == "tool_call_begin"]
    deltas = [e for e in events if e.type == "tool_args_delta"]
    assert len(begins) == 1 and begins[0].name == "run" and begins[0].tool_call_id == "call_1"
    assert [d.text for d in deltas] == ['{"a"', ":1}"]


# ---------- 4. thought_signature 透传（Gemini 多轮必需） ----------


def test_thought_signature_passthrough(transport):
    events = list(transport.to_events([_chunk(tool_calls=[_tc(tc_id="c1", name="t", thought_signature="SIG==")])]))
    begin = [e for e in events if e.type == "tool_call_begin"][0]
    assert begin.thought_signature == "SIG=="


# ---------- 5. reasoning_content ----------


def test_reasoning_delta(transport):
    events = list(transport.to_events([_chunk(reasoning="思考中")]))
    assert events[0].type == "reasoning_delta" and events[0].text == "思考中"


# ---------- 6. finish_reason=None 不发 finish ----------


def test_no_finish_when_none(transport):
    events = list(transport.to_events([_chunk(content="x")]))
    assert not [e for e in events if e.type == "finish"]


# ---------- 7. 空 delta 不产事件 ----------


def test_empty_delta_no_events(transport):
    assert list(transport.to_events([_chunk()])) == []


# ---------- 8. 同一 chunk 含 content 与 tool_calls ----------


def test_content_and_tool_calls_in_same_chunk(transport):
    events = list(
        transport.to_events([_chunk(content="先说明", tool_calls=[_tc(tc_id="c9", name="go", arguments="{}")])])
    )
    types = [e.type for e in events]
    assert types == ["content_delta", "tool_call_begin", "tool_args_delta"]


# ---------- 9. usage 附在普通 chunk ----------


def test_usage_on_normal_chunk(transport):
    usage = SimpleNamespace(prompt_tokens=5, completion_tokens=0, total_tokens=5)
    events = list(transport.to_events([_chunk(content="a", usage=usage)]))
    assert [e.type for e in events] == ["content_delta", "usage"]


# ---------- 10. 错误映射 ----------


def test_classify_error_tool_order(transport):
    assert transport.classify_error("Error code: 400 - 2013 tool call result does not follow tool call") == "tool_order"
    assert transport.classify_error("tool_calls cannot be empty") is None


def test_classify_error_missing_args(transport):
    assert transport.classify_error("Missing required arguments: path") == "missing_args"
    assert transport.classify_error("missing a required argument: query") == "missing_args"
    assert transport.classify_error("rate limit exceeded") is None


# ---------- 11. 请求组装 ----------


def test_build_request_kwargs_basic(transport, monkeypatch):
    monkeypatch.setattr(transport, "_apply_thinking", lambda *a: None)  # 隔离思考映射（另有测试）
    out = transport.build_request_kwargs(
        {"模型名称": "gpt-4o", "API_KEY": "sk-x", "API_URL": "https://x", "最大Token": 100}
    )
    assert out["model"] == "gpt-4o"
    assert out["extra_body"]["max_tokens"] == 100
    # 认证头不在组装结果内（由 worker 统一算后经 create_stream 注入）
    assert "_auth_headers" not in out
    assert out["_is_o1_model"] is False


def test_build_request_kwargs_skips_sampling_params(transport, monkeypatch):
    """温度/top_p 不进 extra_body —— 等价搬迁自原 worker（skip_params 语义，行为不变）"""
    monkeypatch.setattr(transport, "_apply_thinking", lambda *a: None)
    out = transport.build_request_kwargs({"模型名称": "gpt-4o", "温度": 0.5, "top_p": 0.9})
    assert "temperature" not in out["extra_body"]
    assert "top_p" not in out["extra_body"]


def test_build_request_kwargs_o1_skips_sampling(transport, monkeypatch):
    monkeypatch.setattr(transport, "_apply_thinking", lambda *a: None)
    out = transport.build_request_kwargs({"模型名称": "o1-preview", "温度": 0.5, "top_p": 0.9})
    assert out["_is_o1_model"] is True
    assert "temperature" not in out["extra_body"] and "top_p" not in out["extra_body"]


def test_build_request_kwargs_bce_auth_not_in_transport(transport, monkeypatch):
    """认证头组装归 worker（_build_auth_headers 唯一组装点），transport 不再自算"""
    monkeypatch.setattr(transport, "_apply_thinking", lambda *a: None)
    out = transport.build_request_kwargs({"模型名称": "ernie", "API_KEY": "ak", "认证方式": "bce"})
    assert "_auth_headers" not in out


def test_build_request_kwargs_caps_max_tokens(transport, monkeypatch):
    monkeypatch.setattr(transport, "_apply_thinking", lambda *a: None)
    out = transport.build_request_kwargs({"模型名称": "m", "最大Token": 999999}, cap_max_tokens=lambda m, r: 8192)
    assert out["extra_body"]["max_tokens"] == 8192


# ---------- 12. 能力声明与契约 ----------


def test_transport_satisfies_contract(transport):
    from app.plugins.contracts.protocol_transport import ProtocolTransport

    assert isinstance(transport, ProtocolTransport)
    assert transport.id == "openai_chat"
    assert transport.supports_streaming is True


# ---------- 13. token 上限钳制注入（回归：漏注导致 400 错误码 1210） ----------


def test_cap_max_tokens_injected_applies_to_request(transport):
    """回归：worker 须注入 set_cap_max_tokens，否则用户配的极大值直接透传被上游拒。

    实况：MiniMax 限制 max_tokens ∈ [1, 131072]，用户配 200000 → 400 code 1210。
    """
    transport.set_cap_max_tokens(lambda model, req: min(int(req), 65536))
    try:
        out = transport.build_request_kwargs(
            {"模型名称": "MiniMax-M2", "最大Token": 200000}, cap_max_tokens=transport._cap_max_tokens
        )
        assert out["extra_body"]["max_tokens"] == 65536
    finally:
        transport.set_cap_max_tokens(None)


def test_cap_max_tokens_none_passes_through(transport):
    """未注入时不钳制（独立使用场景；worker 路径必须注入）"""
    out = transport.build_request_kwargs(
        {"模型名称": "m", "最大Token": 200000}, cap_max_tokens=transport._cap_max_tokens
    )
    assert out["extra_body"]["max_tokens"] == 200000


# ---------- 14. 模型级流式能力声明（worker 消费，替代模型名硬编码） ----------


def test_supports_streaming_for_o1_family_returns_false(transport):
    """o1/o3 的 chat/completions 不接受 stream 参数 → 声明 False"""
    assert transport.supports_streaming_for({"模型名称": "o1-preview"}) is False
    assert transport.supports_streaming_for({"模型名称": "o3-mini"}) is False


def test_supports_streaming_for_normal_models_returns_true(transport):
    assert transport.supports_streaming_for({"模型名称": "gpt-4o"}) is True
    assert transport.supports_streaming_for({"模型名称": "gpt-5.2"}) is True
    assert transport.supports_streaming_for({}) is True


def test_create_stream_uses_model_level_streaming_flag(transport, monkeypatch):
    """create_stream 的 stream 参数随模型变（o1 场景 False）"""
    captured = {}

    class _Completions:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            return iter(())

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    transport.set_client_factory(lambda: _Client())
    try:
        transport.create_stream({"模型名称": "o1-preview"}, [{"role": "user", "content": "x"}])
        assert captured["stream"] is False
        transport.create_stream({"模型名称": "gpt-4o"}, [{"role": "user", "content": "x"}])
        assert captured["stream"] is True
    finally:
        transport.set_client_factory(None)
