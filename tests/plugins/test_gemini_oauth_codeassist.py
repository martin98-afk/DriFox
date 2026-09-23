# -*- coding: utf-8 -*-
"""gemini-oauth 插件 Code Assist 三件套回归测试（serializer 映射 / SSE 适配 / adapter flags）。

插件目录非包，统一按路径 importlib 加载（对齐运行时真实加载方式）；纯逻辑断言，
不建 QApplication、不依赖全局 Settings 单例。
"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent / "plugins" / "gemini-oauth"


def _load(rel_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, _PLUGIN_ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def serializer():
    module = _load(Path("serializers") / "codeassist.py", "codeassist_serializer_test")
    return module.CodeAssistSerializer()


@pytest.fixture(scope="module")
def adapter_module():
    return _load(Path("model_adapters") / "codeassist.py", "codeassist_adapter_module_test")


@pytest.fixture(scope="module")
def adapter(adapter_module):
    return adapter_module.GeminiCodeAssistAdapter()


@pytest.fixture(scope="module")
def adapter_module():
    return _load(Path("model_adapters") / "codeassist.py", "codeassist_adapter_module_test")


def _serialize(serializer, messages):
    from app.plugins.contracts.message_serializer import SerializeContext
    from app.plugins.contracts.model_adapter import ProtocolFlags

    return serializer.serialize(messages, SerializeContext(supports_vision=True, flags=ProtocolFlags()))


# ---------- 消息映射 ----------


def test_system_message_goes_to_extra_system_instruction(serializer):
    result = _serialize(serializer, [{"role": "system", "content": "你是助手"}])
    assert result.messages == []
    instruction = result.extra["systemInstruction"]
    assert instruction["parts"] == [{"text": "你是助手"}]


def test_user_text_and_tool_flow_roundtrip(serializer):
    messages = [
        {"role": "user", "content": "查天气"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "北京"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "get_weather", "content": "晴 25 度"},
        {"role": "assistant", "content": "今天晴。"},
    ]
    contents = _serialize(serializer, messages).messages

    assert contents[0] == {"role": "user", "parts": [{"text": "查天气"}]}
    assert contents[1]["role"] == "model"
    call = contents[1]["parts"][0]["functionCall"]
    assert call["name"] == "get_weather"
    assert call["args"] == {"city": "北京"}  # arguments 字符串 → dict
    assert contents[2]["role"] == "user"
    response = contents[2]["parts"][0]["functionResponse"]
    assert response["name"] == "get_weather"  # name 经 id 反查路径保留
    assert response["response"] == {"result": "晴 25 度"}
    assert contents[3] == {"role": "model", "parts": [{"text": "今天晴。"}]}


def test_tool_name_missing_falls_back_to_call_names(serializer):
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_9", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_9", "name": "", "content": "ok"},
    ]
    contents = _serialize(serializer, messages).messages
    assert contents[1]["parts"][0]["functionResponse"]["name"] == "lookup"


def test_consecutive_same_role_merged(serializer):
    contents = _serialize(
        serializer,
        [
            {"role": "user", "content": "第一句"},
            {"role": "user", "content": "第二句"},
        ],
    ).messages
    assert len(contents) == 1  # Gemini 要求 user/model 严格交替
    assert contents[0]["parts"] == [{"text": "第一句"}, {"text": "第二句"}]


# ---------- tools 转换 ----------


def test_convert_tools_strips_poison_fields(serializer):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "run",
                "description": "运行",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "cmd": {"type": "string", "default": "ls", "$schema": "http://x", "additionalProperties": False}
                    },
                    "additionalProperties": False,
                    "strict": True,
                },
            },
        }
    ]
    declarations = serializer.convert_tools(tools)
    assert declarations[0]["functionDeclarations"][0]["name"] == "run"
    params = declarations[0]["functionDeclarations"][0]["parameters"]
    assert "$schema" not in json.dumps(params)
    assert "additionalProperties" not in json.dumps(params)
    assert "strict" not in json.dumps(params)
    assert params["properties"]["cmd"]["type"] == "string"


def test_convert_tools_empty_when_no_function(serializer):
    assert serializer.convert_tools([]) == []
    assert serializer.convert_tools([{"type": "function", "function": {}}]) == []


# ---------- SSE → OpenAI chunk 适配 ----------


def _make_stream(serializer_module, sse_lines):
    lines = [f"data: {line}" if not line.startswith("data:") else line for line in sse_lines]
    response = SimpleNamespace(iter_lines=lambda: iter(lines), close=lambda: None)
    return serializer_module.CodeAssistStream(response)


def test_stream_text_and_finish(serializer, serializer_module=None):
    module = _load(Path("serializers") / "codeassist.py", "codeassist_stream_text_test")
    payload = json.dumps(
        {
            "response": {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "你好"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15},
                "modelVersion": "gemini-2.5-flash",
            }
        },
        ensure_ascii=False,
    )
    chunks = list(_make_stream(module, [payload]))
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.choices[0].delta.content == "你好"
    assert chunk.choices[0].finish_reason == "stop"
    assert chunk.usage.prompt_tokens == 10


def test_stream_function_call_becomes_openai_tool_call(serializer):
    module = _load(Path("serializers") / "codeassist.py", "codeassist_stream_tool_test")
    payload = json.dumps(
        {
            "response": {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"functionCall": {"name": "run", "args": {"cmd": "ls"}}}],
                        },
                        "finishReason": "STOP",
                    }
                ]
            }
        },
        ensure_ascii=False,
    )
    chunk = list(_make_stream(module, [payload]))[0]
    tc = chunk.choices[0].delta.tool_calls[0]
    assert tc.function.name == "run"
    assert json.loads(tc.function.arguments) == {"cmd": "ls"}
    assert tc.id.startswith("call_ca_")
    assert chunk.choices[0].delta.content is None


def test_stream_thought_part_maps_to_reasoning(serializer):
    module = _load(Path("serializers") / "codeassist.py", "codeassist_stream_thought_test")
    payload = json.dumps(
        {
            "response": {
                "candidates": [
                    {"content": {"role": "model", "parts": [{"thought": True, "text": "想一下"}, {"text": "答"}]}}
                ]
            }
        },
        ensure_ascii=False,
    )
    chunk = list(_make_stream(module, [payload]))[0]
    assert chunk.choices[0].delta.reasoning_content == "想一下"
    assert chunk.choices[0].delta.content == "答"


def test_stream_max_tokens_maps_to_length_and_usage_only_chunk(serializer):
    module = _load(Path("serializers") / "codeassist.py", "codeassist_stream_finish_test")
    usage_only = json.dumps({"response": {"usageMetadata": {"promptTokenCount": 3}}})
    truncated = json.dumps(
        {
            "response": {
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": "部分"}]}, "finishReason": "MAX_TOKENS"}
                ]
            }
        }
    )
    chunks = list(_make_stream(module, [usage_only, truncated, "data: [DONE]"]))
    assert chunks[0].choices == []  # 空 choices chunk：usage 单独消费（worker 已兼容）
    assert chunks[0].usage.prompt_tokens == 3
    assert chunks[1].choices[0].finish_reason == "length"
    assert "[DONE]" not in str(chunks)  # 非 JSON data 行被安全跳过


# ---------- adapter 判定与 flags ----------


def test_adapter_matches_by_url_and_provider_mark(adapter):
    assert adapter.matches({"API_URL": "https://cloudcode-pa.googleapis.com/v1internal"}) == 100
    assert adapter.matches({"API_URL": "https://x.com", "provider_name": "Google Gemini (OAuth)"}) == 90
    assert adapter.matches({"API_URL": "https://api.deepseek.com"}) == 0
    assert adapter.matches({}) == 0


def test_adapter_flags_declare_codeassist_protocol(adapter):
    flags = adapter.protocol_flags({"API_KEY": ""})  # 无 token → ensure_project 静默跳过
    assert flags.protocol == "codeassist"
    assert flags.serializer_id == "codeassist"
    assert flags.is_gemini is True
    assert "project" not in flags.extra


def test_adapter_flags_declare_codeassist_protocol(adapter):
    flags = adapter.protocol_flags({"API_KEY": ""})  # 无 token → ensure_project 静默跳过
    assert flags.protocol == "codeassist"
    assert flags.serializer_id == "codeassist"
    assert flags.is_gemini is True
    assert "project" not in flags.extra


def test_adapter_flags_extra_carries_project_and_transport(adapter, adapter_module, monkeypatch):
    class _FakeProvider:
        @staticmethod
        def ensure_project(refresh_token):
            assert refresh_token == "rt-abc"
            return "proj-1"

    monkeypatch.setattr(adapter_module, "_load_provider_module", lambda: _FakeProvider())
    flags = adapter.protocol_flags({"API_KEY": "rt-abc"})
    assert flags.extra["project"] == "proj-1"
    # transport 随 flags 携带（worker 分派唯一依据），且满足 ProtocolTransport 契约
    from app.plugins.contracts.protocol_transport import ProtocolTransport

    transport = flags.extra["transport"]
    assert isinstance(transport, ProtocolTransport)
    assert callable(transport.create_stream)


# ---------- transport 请求构造 ----------


@pytest.fixture(scope="module")
def registered_serializer(serializer):
    """transport.build_request 内部经 registry resolve serializer——预先注册，结束后清理"""
    from app.plugins.registries.serializer_registry import SerializerRegistry

    registry = SerializerRegistry.get_instance()
    registry.register(serializer)
    yield serializer
    registry.unregister_source("")


def test_generation_config_maps_params(adapter_module):
    cfg = adapter_module.CodeAssistTransport._generation_config(
        {"温度": 0.5, "top_p": 0.9, "最大Token": 999999, "思考模式": True, "思考预算": 4096}
    )
    assert cfg["temperature"] == 0.5
    assert cfg["topP"] == 0.9
    assert cfg["maxOutputTokens"] <= 65536  # 绝对上限保护
    assert cfg["thinkingConfig"] == {"thinkingBudget": 4096}

    off = adapter_module.CodeAssistTransport._generation_config({"思考模式": False})
    assert off["thinkingConfig"] == {"thinkingBudget": 0}
    assert "temperature" not in off


def test_build_request_assembles_codeassist_body(adapter_module, registered_serializer, monkeypatch):
    class _FakeProvider:
        @staticmethod
        def ensure_project(refresh_token):
            assert refresh_token == "rt-x"
            return "proj-9"

    monkeypatch.setattr(adapter_module, "_load_provider_module", lambda: _FakeProvider())
    transport = adapter_module.CodeAssistTransport()
    url, headers, body = transport.build_request(
        {
            "API_KEY": "rt-x",
            "API_URL": "https://cloudcode-pa.googleapis.com/v1internal/",
            "模型名称": "gemini-2.5-flash",
        },
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "t1", "description": "d", "parameters": {"type": "object"}}}],
        auth_headers={"Authorization": "Bearer at"},
    )
    assert url == "https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse"  # 尾斜杠已去
    assert headers["Authorization"] == "Bearer at"
    assert body["project"] == "proj-9"
    assert body["model"] == "gemini-2.5-flash"
    request_payload = body["request"]
    assert request_payload["contents"][0] == {"role": "user", "parts": [{"text": "hi"}]}
    assert request_payload["systemInstruction"]["parts"] == [{"text": "sys"}]
    assert request_payload["tools"][0]["functionDeclarations"][0]["name"] == "t1"
