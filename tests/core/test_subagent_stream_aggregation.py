# -*- coding: utf-8 -*-
"""子智能体 chat/completions 分支：流式聚合 + 服务商请求头注入。

## 背景（Bug）
CodeBuddy（copilot.tencent.com）端点仅支持流式请求，非流式直接 400：
    Error code: 400 - Non-stream chat request is currently not supported

子智能体链路（_make_api_call）此前固定 `stream: False`，用 CodeBuddy 当
子智能体/团队模型时任务必失败；且重试对确定性 400 无效（15 轮全同）。

同时 _make_api_call 不注入服务商插件声明的 capabilities["extra_headers"]
（CodeBuddy 的 X-Product-Code / User-Agent / 动态 access_token），
即使改成流式也会在身份校验阶段被拒。

## 断言
1. chat/completions 请求必须 stream=True（仅流式端点兼容）
2. extra_headers 经 resolve_extra_headers 注入请求（伪装身份 + 动态 token）
3. 流式分片聚合还原 (content, tool_calls, reasoning)：
   - content / reasoning_content 按序拼接
   - tool_calls 首片带 id+name、后续片仅 index+arguments → 聚合为完整参数串
4. usage 记账（含流末尾独立 usage chunk）
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.workers.subagent_worker import SubAgentExecutor


def _chunk(content=None, reasoning=None, tool_calls=None, usage=None, finish_reason=None):
    """构造一个最小 OpenAI ChatCompletionChunk 形状的对象。"""
    delta = None
    if content is not None or reasoning is not None or tool_calls is not None:
        delta = SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls)
    choices = []
    if delta is not None or finish_reason is not None:
        choices = [SimpleNamespace(delta=delta, finish_reason=finish_reason)]
    return SimpleNamespace(choices=choices, usage=usage)


def _tool_call_delta(index, call_id=None, name=None, arguments=None):
    func = None
    if name is not None or arguments is not None:
        func = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=call_id, function=func)


def _build_executor(**cfg) -> SubAgentExecutor:
    llm_config = {"API_KEY": "sk-test", "模型名称": "glm-5.3-flash", "API_URL": "https://copilot.tencent.com/v2"}
    llm_config.update(cfg)
    return SubAgentExecutor(
        task_id="t_stream",
        agent_name="build",
        task_description="test",
        llm_config=llm_config,
        agent_manager=None,
        tool_executor=None,
    )


@pytest.fixture()
def capture_create(monkeypatch):
    """替换 build_openai_client，捕获 chat.completions.create 的 kwargs 与流"""
    captured = {"kwargs": None, "stream": None}

    def _fake_create(**kwargs):
        captured["kwargs"] = kwargs
        return captured["stream"]

    client = MagicMock()
    client.chat.completions.create.side_effect = _fake_create
    monkeypatch.setattr("app.utils.http_client.build_openai_client", lambda **kw: client)
    return captured


def _ensure_plugins_loaded():
    from app.plugins.loaders.runtime_component_loader import warmup_runtime_components

    warmup_runtime_components()


# ── 1. 必须流式发起（CodeBuddy 仅流式端点）──────────────


def test_chat_completions_uses_stream(capture_create):
    """CodeBuddy 等仅流式端点：请求必须 stream=True"""
    _ensure_plugins_loaded()
    capture_create["stream"] = iter([_chunk(content="hi", finish_reason="stop")])
    ex = _build_executor()
    ex._make_api_call([{"role": "user", "content": "x"}], llm_config=dict(ex.llm_config))

    assert captured_stream(capture_create) is True, "子智能体必须流式发起，否则仅流式端点 400 且重试无效"


def captured_stream(captured) -> bool:
    kwargs = captured["kwargs"] or {}
    return kwargs.get("stream")


# ── 2. 服务商请求头注入 ─────────────────────────────


def test_provider_extra_headers_injected(capture_create, monkeypatch):
    """CodeBuddy 伪装头 + 动态 access_token 必须随请求注入"""
    _ensure_plugins_loaded()
    capture_create["stream"] = iter([_chunk(content="hi", finish_reason="stop")])
    monkeypatch.setattr(
        "app.core.workers.subagent_worker.resolve_extra_headers",
        lambda cfg, session_id="": {
            "User-Agent": "CodeBuddyIDE/1.106.1",
            "X-Product-Code": "codebuddy",
            "Authorization": "Bearer test-access",
        },
    )
    ex = _build_executor()
    ex._make_api_call([{"role": "user", "content": "x"}], llm_config=dict(ex.llm_config))

    headers = (capture_create["kwargs"] or {}).get("extra_headers") or {}
    assert headers.get("X-Product-Code") == "codebuddy"
    assert headers.get("User-Agent") == "CodeBuddyIDE/1.106.1"
    assert headers.get("Authorization") == "Bearer test-access"


def test_no_extra_headers_when_provider_silent(capture_create):
    """服务商未声明请求头 → 不发 extra_headers（不透传 None）"""
    _ensure_plugins_loaded()
    capture_create["stream"] = iter([_chunk(content="hi", finish_reason="stop")])
    ex = _build_executor(API_URL="https://api.example.com/v1")
    ex._make_api_call([{"role": "user", "content": "x"}], llm_config=dict(ex.llm_config))

    assert "extra_headers" not in (capture_create["kwargs"] or {})


# ── 3. 流式聚合 ─────────────────────────────────────


def test_content_and_reasoning_aggregated(capture_create):
    """content / reasoning_content 按分片顺序拼接"""
    _ensure_plugins_loaded()
    capture_create["stream"] = iter(
        [
            _chunk(reasoning="让我想"),
            _chunk(reasoning="一下"),
            _chunk(content="你好"),
            _chunk(content="，世界"),
            _chunk(finish_reason="stop"),
        ]
    )
    ex = _build_executor()
    content, tool_calls, reasoning = ex._make_api_call(
        [{"role": "user", "content": "x"}], llm_config=dict(ex.llm_config)
    )

    assert content == "你好，世界"
    assert reasoning == "让我想一下"
    assert tool_calls == []


def test_tool_call_fragments_aggregated(capture_create):
    """tool_calls 分片：首片 id+name，后续片 index+arguments 增量 → 完整参数串"""
    _ensure_plugins_loaded()
    capture_create["stream"] = iter(
        [
            _chunk(tool_calls=[_tool_call_delta(0, call_id="call_1", name="bash")]),
            _chunk(tool_calls=[_tool_call_delta(0, arguments='{"com')]),
            _chunk(tool_calls=[_tool_call_delta(0, arguments='mand": "ls"}')]),
            # 第二个并行调用，index=1
            _chunk(tool_calls=[_tool_call_delta(1, call_id="call_2", name="read")]),
            _chunk(tool_calls=[_tool_call_delta(1, arguments='{"path": "a.py"}')]),
            _chunk(finish_reason="tool_calls"),
        ]
    )
    ex = _build_executor()
    content, tool_calls, reasoning = ex._make_api_call(
        [{"role": "user", "content": "x"}], llm_config=dict(ex.llm_config)
    )

    assert content == ""
    assert len(tool_calls) == 2
    assert tool_calls[0]["id"] == "call_1"
    assert tool_calls[0]["function"]["name"] == "bash"
    assert tool_calls[0]["function"]["arguments"] == '{"command": "ls"}'
    assert tool_calls[1]["id"] == "call_2"
    assert tool_calls[1]["function"]["name"] == "read"
    assert tool_calls[1]["function"]["arguments"] == '{"path": "a.py"}'


def test_tool_call_without_id_gets_synthetic_id(capture_create):
    """部分网关只在首片给 id 且可能缺失 → 兜底合成 id，保证 tool 消息可回填"""
    _ensure_plugins_loaded()
    capture_create["stream"] = iter(
        [
            _chunk(tool_calls=[_tool_call_delta(0, name="bash")]),
            _chunk(tool_calls=[_tool_call_delta(0, arguments="{}")]),
        ]
    )
    ex = _build_executor()
    _, tool_calls, _ = ex._make_api_call([{"role": "user", "content": "x"}], llm_config=dict(ex.llm_config))

    assert len(tool_calls) == 1
    assert tool_calls[0]["id"], "缺 id 时必须合成兜底 id"
    assert tool_calls[0]["function"]["name"] == "bash"


def test_usage_recorded_from_trailing_chunk(capture_create):
    """流末尾独立 usage chunk（choices 为空）也必须记账"""
    _ensure_plugins_loaded()
    capture_create["stream"] = iter(
        [
            _chunk(content="hi"),
            _chunk(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)),
            _chunk(finish_reason="stop"),
        ]
    )
    ex = _build_executor()
    emitted = []
    ex.token_usage_updated.connect(lambda tid, pt, ct, tt: emitted.append((pt, ct, tt)))
    ex._make_api_call([{"role": "user", "content": "x"}], llm_config=dict(ex.llm_config))

    assert emitted == [(10, 5, 15)]
    assert ex._total_tokens == 15


def test_empty_stream_returns_empty_result(capture_create):
    """空流不抛异常（防御性：部分网关异常关闭）"""
    _ensure_plugins_loaded()
    capture_create["stream"] = iter([])
    ex = _build_executor()
    content, tool_calls, reasoning = ex._make_api_call(
        [{"role": "user", "content": "x"}], llm_config=dict(ex.llm_config)
    )

    assert content == "" and tool_calls == [] and reasoning == ""
