# -*- coding: utf-8 -*-
"""Responses API（GPT-5.x 系列）流式解析与消息转换测试。

背景：gpt-5.6-luna 等 GPT-5.x 模型的思考只在 Responses API
（/v1/responses）的 reasoning_summary_text 事件返回，chat/completions
流式 delta 无 reasoning 字段 → chat_worker 走 _process_responses_stream
解析事件并复用既有信号（reasoning_content_received / content_received /
tool_call_started 等）。
"""

import json

import pytest

from app.core.message_content import messages_to_responses_input
from app.core.workers.chat_worker import OpenAIChatWorker
from app.core.workers.subagent_worker import SubAgentExecutor


# ---------- 消息转换 ----------


class TestMessagesToResponsesInput:
    def test_basic_conversion(self):
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
            {"role": "user", "content": "谢谢"},
        ]
        items, instructions = messages_to_responses_input(messages)
        assert instructions == "你是助手"
        # system 不进 input
        assert all(it.get("role") != "system" for it in items)
        assert items[0] == {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "你好"}]}

    def test_tool_call_conversion(self):
        messages = [
            {"role": "user", "content": "北京天气？"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"北京"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "get_weather", "content": "晴 25度"},
        ]
        items, _ = messages_to_responses_input(messages)
        fc = [it for it in items if it.get("type") == "function_call"]
        out = [it for it in items if it.get("type") == "function_call_output"]
        assert len(fc) == 1
        assert fc[0] == {
            "type": "function_call",
            "call_id": "call_1",
            "name": "get_weather",
            "arguments": '{"city":"北京"}',
        }
        assert out[0] == {"type": "function_call_output", "call_id": "call_1", "output": "晴 25度"}

    def test_image_conversion(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                ],
            }
        ]
        items, _ = messages_to_responses_input(messages, supports_vision=True)
        parts = items[0]["content"]
        assert parts[1] == {"type": "input_image", "image_url": "data:image/png;base64,AAA"}
        # 不支持视觉 → 图片替换占位
        items2, _ = messages_to_responses_input(messages, supports_vision=False)
        parts2 = items2[0]["content"]
        assert parts2[1] == {"type": "input_text", "text": "[图片]"}


# ---------- 流式事件解析 ----------


class FakeEvent:
    """模拟 Responses API 流式事件对象（openai SDK 事件的最小形态）"""

    def __init__(self, etype, **kwargs):
        self.type = etype
        for k, v in kwargs.items():
            setattr(self, k, v)


def make_worker():
    worker = OpenAIChatWorker(
        messages=[{"role": "user", "content": "hi"}],
        session_messages=[],
        llm_config={"模型名称": "gpt-5.6-luna", "API_URL": "http://fake", "API_KEY": ""},
        stream=True,
    )
    worker.received_reasoning = []
    worker.received_content = []
    worker.thinking_count = [0]
    worker.tool_starts = []
    worker.tool_args = []
    worker.reasoning_content_received.connect(lambda p: worker.received_reasoning.append(p))
    worker.content_received.connect(lambda p: worker.received_content.append(p))
    worker.thinking_started.connect(lambda: worker.thinking_count.__setitem__(0, worker.thinking_count[0] + 1))
    worker.tool_call_started.connect(lambda cid, name, args, stage: worker.tool_starts.append((cid, name, args, stage)))
    worker.tool_args_updated.connect(lambda cid, name, args: worker.tool_args.append((cid, name, args)))
    return worker


class FakeResponsesStream:
    """模拟 client.responses.create(stream=True) 返回的可迭代对象"""

    def __init__(self, events):
        self._events = events

    def __iter__(self):
        return iter(self._events)


class TestProcessResponsesStream:
    def test_reasoning_and_content(self):
        worker = make_worker()
        events = [
            FakeEvent("response.reasoning_summary_text.delta", item_id="rs_1", delta="先算乘法"),
            FakeEvent("response.reasoning_summary_text.delta", item_id="rs_1", delta="再算除法"),
            FakeEvent("response.output_text.delta", item_id="msg_1", delta="答案是"),
            FakeEvent("response.output_text.delta", item_id="msg_1", delta=" 399"),
            FakeEvent("response.completed"),
        ]
        found, pending = worker._process_responses_stream(FakeResponsesStream(events))
        assert found is False
        assert pending is True
        assert worker.thinking_count[0] == 1
        assert "".join(worker.received_reasoning) == "先算乘法再算除法"
        assert "".join(worker.received_content) == "答案是 399"

    def test_tool_call_flow(self):
        worker = make_worker()
        events = [
            FakeEvent(
                "response.output_item.added",
                item={"id": "fc_1", "type": "function_call", "name": "get_weather", "arguments": ""},
            ),
            FakeEvent("response.function_call_arguments.delta", item_id="fc_1", delta='{"city":'),
            FakeEvent("response.function_call_arguments.delta", item_id="fc_1", delta='"北京"}'),
            FakeEvent(
                "response.output_item.done",
                item={
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_abc",
                    "name": "get_weather",
                    "arguments": '{"city":"北京"}',
                },
            ),
            FakeEvent("response.completed"),
        ]
        found, pending = worker._process_responses_stream(FakeResponsesStream(events))
        assert found is True
        assert pending is False
        assert "call_abc" in worker._current_tool_calls
        tc = worker._current_tool_calls["call_abc"]
        assert tc["function"]["name"] == "get_weather"
        assert json.loads(tc["function"]["arguments"]) == {"city": "北京"}
        # preview 阶段 tool_call_started + 参数更新已发射
        assert any(cid == "call_abc" for cid, _, _, _ in worker.tool_starts)
        assert any(cid == "call_abc" for cid, _, _ in worker.tool_args)

    def test_failed_event_raises(self):
        worker = make_worker()
        events = [FakeEvent("response.failed", response={"error": {"message": "upstream error", "code": "x"}})]
        with pytest.raises(Exception):
            worker._process_responses_stream(FakeResponsesStream(events))

    def test_empty_response_raises(self):
        worker = make_worker()
        events = [FakeEvent("response.in_progress")]
        with pytest.raises(Exception):
            worker._process_responses_stream(FakeResponsesStream(events))

    def test_cancel_flushes_batches(self):
        worker = make_worker()
        worker._is_cancelled = True
        events = [FakeEvent("response.reasoning_summary_text.delta", item_id="rs_1", delta="思考中")]
        found, pending = worker._process_responses_stream(FakeResponsesStream(events))
        assert found is False
        # 取消路径返回 (False, False)
        assert pending is False


# ---------- SubAgentExecutor 非流式 Responses 解析 ----------


class _FakeItem:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeUsage:
    def __init__(self):
        self.input_tokens = 10
        self.output_tokens = 20
        self.total_tokens = 30


class _FakeSubResp:
    def __init__(self, output, usage=None):
        self.output = output
        self.usage = usage


class TestSubAgentResponses:
    def _make_subagent(self):
        w = SubAgentExecutor.__new__(SubAgentExecutor)
        w._total_prompt_tokens = 0
        w._total_completion_tokens = 0
        w._total_tokens = 0
        w._peak_total_tokens = 0
        w.task_id = "t1"
        w.token_usage_updated = type("S", (), {"emit": lambda *a, **k: None})()
        return w

    def test_parse_output(self):
        w = self._make_subagent()
        resp = _FakeSubResp(
            output=[
                _FakeItem(type="reasoning", summary=[_FakeItem(type="summary_text", text="先算乘法")]),
                _FakeItem(type="reasoning", summary=[{"type": "summary_text", "text": "再算除法"}]),
                _FakeItem(type="message", content=[_FakeItem(type="output_text", text="答案是 399")]),
                _FakeItem(type="function_call", call_id="call_1", name="get_weather", arguments='{"city":"北京"}'),
            ],
            usage=_FakeUsage(),
        )
        content, tool_calls, reasoning = w._parse_responses_output(resp)
        assert content == "答案是 399"
        assert reasoning == "先算乘法再算除法"
        assert tool_calls == [
            {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"北京"}'}}
        ]
        assert w._total_tokens == 30

    def test_use_responses_api_judge(self):
        w = self._make_subagent()
        assert w._use_responses_api({"模型名称": "gpt-5.6-luna"}) is True
        assert w._use_responses_api({"模型名称": "deepseek-v4-flash"}) is False
        # 强制覆盖
        assert w._use_responses_api({"模型名称": "deepseek-v4-flash", "使用ResponsesAPI": True}) is True
        assert w._use_responses_api({"模型名称": "gpt-5.6-luna", "使用ResponsesAPI": False}) is False

    def test_responses_tools_conversion(self):
        tools = [
            {
                "type": "function",
                "function": {"name": "read", "description": "读文件", "parameters": {"type": "object"}},
            }
        ]
        out = SubAgentExecutor._responses_tools(tools)
        assert out == [
            {
                "type": "function",
                "name": "read",
                "description": "读文件",
                "parameters": {"type": "object"},
            }
        ]
