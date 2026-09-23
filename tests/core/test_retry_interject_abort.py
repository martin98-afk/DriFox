# -*- coding: utf-8 -*-
"""回归测试：用户插话打断 API 自动重试

背景：
- chat_worker._make_api_call 内部有 15 次退避重试（5/10/15…s，累计最长 600s）。
- 修复前：重试期间 worker 阻塞在该函数内，_hook_message_queue 只在每轮 API 调用
  前由 _inject_pending_hook_messages 消费 → 插话要等重试全部结束才生效，
  表现为「发了消息 AI 长时间无响应」。
- 修复后：重试退避等待循环与每轮 attempt 开头都会探测插话，命中即放弃剩余重试。
  返回 (None, None) 与取消路径同形 → 主循环走完成路径把已接收内容落库，
  再由 _drain_pending_hooks_before_exit 注入插话并续跑一轮。
"""

import queue
from types import SimpleNamespace

import httpx
from openai import APIError

from app.core.workers.chat_worker import OpenAIChatWorker


class _SignalStub:
    """替代 PyQt 信号的轻量桩，仅记录 emit 调用。"""

    def __init__(self):
        self.emitted = []

    def emit(self, *args, **kwargs):
        self.emitted.append(args)


class _FakeClient:
    """前 fail_times 次调用抛指定错误，之后成功。记录调用次数。"""

    def __init__(self, error_message, fail_times=1):
        self.error_message = error_message
        self.fail_times = fail_times
        self.calls = 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise APIError(
                message=self.error_message,
                request=httpx.Request("POST", "https://api.example/v1"),
                body=None,
            )
        return "ok"


def _make_worker(fake_client, hook_queue, monkeypatch):
    """构造最小可用的 ChatWorker（绕过 __init__），驱动 _make_api_call 重试路径。"""
    worker = OpenAIChatWorker.__new__(OpenAIChatWorker)
    worker.llm_config = {
        "API_KEY": "test-key",
        "API_URL": "https://api.openai.com/v1",
        "模型名称": "gpt-4o",
    }
    worker._supports_vision = False
    worker._api_messages_cache = None
    worker._api_messages_built = False
    worker._current_response = None
    worker._partial_content_backup = None
    worker._response_content_blocks = []
    worker._response_chunks = []
    worker.session_id = None
    worker.tools = None
    worker._is_cancelled = False
    worker.retry_resolved = _SignalStub()
    worker.retry_status = _SignalStub()
    worker._http_client = None
    worker.tool_executor = SimpleNamespace(_backend=SimpleNamespace(_hook_message_queue=hook_queue))
    # 协议通道已插件化：直接把假 transport/sink 挂到 worker（原 _build_api_request_kwargs /
    # _get_http_client / _process_response 的 mock 点已不存在）。
    # 不走注册表：session 级 warmup 会在跨文件运行时注册真插件，注册顺序不可控。
    from tests.core.protocol_test_helpers import FakeChatTransport, FakeStreamSink

    worker._chat_transport = FakeChatTransport(fake_client)
    worker._stream_sink_override = FakeStreamSink(result=(True, True))
    # 加速：重试退避不真睡（真实逻辑每 0.5s 检查一次取消/插话标志）
    monkeypatch.setattr("app.core.workers.chat_worker.time.sleep", lambda s: None)
    return worker


def test_interject_during_retry_wait_aborts_retry(monkeypatch):
    """核心场景：重试退避等待期间有插话 → 立即放弃剩余重试，不再打第二次 API。"""
    hook_q = queue.Queue()
    hook_q.put({"role": "user", "content": "插话", "_interject": True})
    client = _FakeClient("Streaming response failed: [503] The request queue is full.")
    worker = _make_worker(client, hook_q, monkeypatch)

    result = worker._make_api_call([{"role": "user", "content": "hi"}])

    assert client.calls == 1, f"插话应掐断重试，实际调用 {client.calls} 次"
    assert result == (None, None)
    # 探测不消费：插话仍留在队列，交给循环顶部 _inject_pending_hook_messages 注入
    assert hook_q.qsize() == 1, "探测插话不得消费队列"
    # 通知 UI 收掉重试动画
    assert worker.retry_resolved.emitted, "应发射 retry_resolved 以结束重试动画"


def test_no_interject_keeps_retrying(monkeypatch):
    """对照：队列为空时不改变原有重试行为。"""
    client = _FakeClient("Streaming response failed: [503] The request queue is full.")
    worker = _make_worker(client, queue.Queue(), monkeypatch)

    result = worker._make_api_call([{"role": "user", "content": "hi"}])

    assert client.calls == 2, f"无插话应正常重试，实际调用 {client.calls} 次"
    assert result == (True, True)


def test_non_interject_hook_does_not_abort_retry(monkeypatch):
    """对照：TeamMail 等其他 hook 条目不应掐断重试（只认 _interject 标记）。"""
    hook_q = queue.Queue()
    hook_q.put({"role": "user", "content": "📨 团队邮件", "_hook_event": "TeamMail"})
    client = _FakeClient("Streaming response failed: [503] The request queue is full.")
    worker = _make_worker(client, hook_q, monkeypatch)

    result = worker._make_api_call([{"role": "user", "content": "hi"}])

    assert client.calls == 2, f"非插话条目不应中断重试，实际调用 {client.calls} 次"
    assert result == (True, True)
