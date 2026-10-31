# -*- coding: utf-8 -*-
"""真实构造全链复现：空闲期 PluginChanged 入队 → 下次对话补注入。

与 test_plugin_changed_idle_reinject.py（__new__ 桩）不同：本测试走真实
OpenAIChatWorker.__init__ + 真实序列化器插件 + 真实 run() 主循环，只 mock 网络
transport/sink。用于定位「同一次运行内丢失」的真实断点。
"""

import queue

import pytest

from tests.core.protocol_test_helpers import FakeChatTransport, FakeStreamSink, install_fake_chat_plugins

pytestmark = pytest.mark.usefixtures("qapp")


class _FakeClient:
    """假 API client：返回空流（真实流式事件由 FakeStreamSink 预设）"""

    def __init__(self):
        self.calls = 0
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return []


@pytest.fixture
def fake_plugins(monkeypatch):
    """假 transport/sink + 假 client 捕获请求消息（registry 冷启动由 worker 兜底）"""
    client = _FakeClient()
    guard = install_fake_chat_plugins(client, response_result=(False, False))
    # 捕获 transport 收到的 messages
    orig_create_stream = FakeChatTransport.create_stream
    captured = {}

    def _capture_create_stream(self, llm_config, messages, tools=None, auth_headers=None, api_messages=None):
        captured["messages"] = list(api_messages if api_messages is not None else messages)
        return orig_create_stream(self, llm_config, messages, tools, auth_headers, api_messages)

    FakeChatTransport.create_stream = _capture_create_stream
    try:
        yield client, captured
    finally:
        FakeChatTransport.create_stream = orig_create_stream
        guard.restore()


def test_real_worker_injects_idle_plugin_changed(fake_plugins):
    """空闲入队的 PluginChanged 消息 → 真实 worker run() 第一轮注入 API 请求。"""
    client, captured = fake_plugins
    from app.core.workers.chat_worker import OpenAIChatWorker

    hook_q = queue.Queue()
    hook_q.put(
        {
            "role": "user",
            "content": "<system-reminder><plugin-changed-hook>插件 X 已更新：tools_added=[foo]</plugin-changed-hook></system-reminder>",
            "_hook_event": "PluginChanged",
        }
    )

    backend_stub = type("B", (), {"_hook_message_queue": hook_q, "_window_id": None})()
    tool_executor = type("T", (), {"_backend": backend_stub})()

    worker = OpenAIChatWorker(
        messages=[{"role": "user", "content": "你好"}],
        session_messages=[{"role": "user", "content": "你好"}],
        llm_config={
            "API_KEY": "test-key",
            "API_URL": "https://api.openai.com/v1",
            "模型名称": "gpt-4o",
            "provider_name": "",
        },
        tools=[],
        tool_executor=tool_executor,
        session_id="sess_idle_inject",
    )

    worker.run()

    assert client.calls >= 1, "应至少发起一次 API 调用"
    api_msgs = captured.get("messages") or []
    assert any("plugin-changed-hook" in str(m.get("content", "")) for m in api_msgs), (
        f"PluginChanged 消息应进入 API 请求，实际请求消息: "
        f"{[str(m.get('content', ''))[:60] for m in api_msgs]}"
    )
