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


def test_build_request_kwargs_skips_sampling_params(transport, monkeypatch):
    """温度/top_p 不进 extra_body —— 等价搬迁自原 worker（skip_params 语义，行为不变）"""
    monkeypatch.setattr(transport, "_apply_thinking", lambda *a: None)
    out = transport.build_request_kwargs({"模型名称": "gpt-4o", "温度": 0.5, "top_p": 0.9})
    assert "temperature" not in out["extra_body"]
    assert "top_p" not in out["extra_body"]


def test_build_request_kwargs_o1_skips_sampling(transport, monkeypatch):
    monkeypatch.setattr(transport, "_apply_thinking", lambda *a: None)
    out = transport.build_request_kwargs({"模型名称": "o1-preview", "温度": 0.5, "top_p": 0.9})
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
    """回归：worker 逐请求传入 cap_max_tokens，否则用户配的极大值直接透传被上游拒。

    实况：MiniMax 限制 max_tokens ∈ [1, 131072]，用户配 200000 → 400 code 1210。
    """
    out = transport.build_request_kwargs(
        {"模型名称": "MiniMax-M2", "最大Token": 200000},
        cap_max_tokens=lambda model, req: min(int(req), 65536),
    )
    assert out["extra_body"]["max_tokens"] == 65536


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

    transport.create_stream({"模型名称": "o1-preview"}, [{"role": "user", "content": "x"}], client=_Client())
    assert captured["stream"] is False
    transport.create_stream({"模型名称": "gpt-4o"}, [{"role": "user", "content": "x"}], client=_Client())
    assert captured["stream"] is True


# ---------- 9. 非流式 ChatCompletion 归一（o1/o3 stream=False） ----------


def _completion(content=None, tool_calls=None, reasoning=None):
    """stream=False 时 SDK 返回的单对象形状（choices[0].message 携带最终态）"""
    message = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None)


def test_non_stream_completion_yields_content_only(transport):
    """非流式单对象 → content_delta；不发 usage/finish（sink 非流式分支负责记账）"""
    events = list(transport.to_events(_completion(content="你好")))
    assert [(e.type, e.text) for e in events] == [("content_delta", "你好")]


def test_non_stream_completion_reasoning_before_content(transport):
    """思考先于正文 emit，保持 UI 思考块在前"""
    events = list(transport.to_events(_completion(content="答", reasoning="想")))
    assert [e.type for e in events] == ["reasoning_delta", "content_delta"]


def test_non_stream_completion_tool_calls_normalized_as_begin_args_pair(transport):
    """tool_call 归一为 begin+args_delta 对（sink 按 begin 建 buffer，args 走既有解析路径）"""
    tc = SimpleNamespace(
        index=0, id="call_1", function=SimpleNamespace(name="read", arguments='{"p":1}'), thought_signature="sig"
    )
    events = list(transport.to_events(_completion(tool_calls=[tc])))
    assert [(e.type, e.tool_call_id, e.name, e.text) for e in events] == [
        ("tool_call_begin", "call_1", "read", ""),
        ("tool_args_delta", "call_1", "read", '{"p":1}'),
    ]
    assert events[0].thought_signature == "sig"


def test_non_stream_completion_missing_id_falls_back_to_index(transport):
    tc = SimpleNamespace(index=2, id="", function=SimpleNamespace(name="ls", arguments="{}"), thought_signature="")
    events = list(transport.to_events(_completion(tool_calls=[tc])))
    assert events and all(e.tool_call_id == "index_2" for e in events)


def test_non_stream_completion_unnamed_tool_call_skipped(transport):
    """无名的完整调用无法建 buffer（与流式孤立 delta 同规则），跳过"""
    tc = SimpleNamespace(index=0, id="call_x", function=SimpleNamespace(name="", arguments="{}"), thought_signature="")
    assert list(transport.to_events(_completion(tool_calls=[tc]))) == []


def test_non_stream_completion_empty_choices_yields_no_events(transport):
    """空响应 → 空事件流，交给 sink 的非流式 usage 分支与空响应检测兜底"""
    assert list(transport.to_events(SimpleNamespace(choices=[]))) == []


def test_non_stream_end_to_end_via_create_stream(transport):
    """端到端：o1 配置 → create(stream=False) 返回单对象 → 归一为 content 事件"""

    class _Completions:
        @staticmethod
        def create(**kwargs):
            assert kwargs["stream"] is False
            return _completion(content="非流式回复")

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    stream = transport.create_stream(
        {"模型名称": "o1-preview"}, [{"role": "user", "content": "x"}], client=_Client()
    )
    events = list(stream)
    assert [e.type for e in events] == ["content_delta"]

# ---------- 15. 并发隔离（回归：transport 共享单例被多 worker 互相覆盖） ----------


def test_per_request_client_never_touches_shared_instance(transport):
    """per-request 入参不落实例：连续两次不同 client 调用后，实例仍时不持有任何 client。

    历史缺陷：worker 调 set_client_factory 把本 worker 的 client 写进共享单例，
    并发 worker 互相覆盖 → 请求编程到别家服务商端点（MiniMax 端点收到 glm-5.3-flash）。
    """
    used = []

    def _make_client(tag):
        class _Completions:
            @staticmethod
            def create(**kwargs):
                used.append(tag)
                return iter(())

        class _Chat:
            completions = _Completions()

        class _Client:
            chat = _Chat()

        return _Client()

    transport.create_stream({"模型名称": "m-a"}, [], client=_make_client("A"))
    transport.create_stream({"模型名称": "m-b"}, [], client=_make_client("B"))
    assert used == ["A", "B"]
    # 关键断言：实例上不得沉淀 client 或钳制函数
    assert transport._client_factory is None
    assert transport._cap_max_tokens is None


def test_concurrent_requests_keep_own_client(transport):
    """并发：多线程各传自己的 client，出发的请求必须命中自己的（不被其他线程抢走）。

    模拟原缺陷：set_client_factory 方式下 A 写完 B 写，A 的请求会用 B 的 client。
    """
    import threading

    N = 8
    barrier = threading.Barrier(N)
    seen = {}
    errors = []

    def _client_for(name):
        class _Completions:
            @staticmethod
            def create(**kwargs):
                # 请求发出时回读当前线程持有的名字
                seen.setdefault(name, set()).add(kwargs.get("model"))
                return iter(())

        class _Chat:
            completions = _Completions()

        class _Client:
            chat = _Chat()

        return _Client()

    def _worker(idx):
        name = f"w{idx}"
        try:
            barrier.wait(timeout=5)
            transport.create_stream({"模型名称": name}, [], client=_client_for(name))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(N)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)

    assert not errors, errors
    # 每个 worker 的 client 只应发出自己的模型名
    for i in range(N):
        assert seen.get(f"w{i}") == {f"w{i}"}, seen
