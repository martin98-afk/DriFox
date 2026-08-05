# -*- coding: utf-8 -*-
"""回归测试：SSE 流内 503「请求队列已满」错误加入自动重试

背景：
- MiniMax 等服务端在 SSE 流内返回错误事件（如 "Streaming response failed:
  [503] The request queue is full."），openai SDK 将其包装为通用 APIError
  （无 status_code 属性），而非 InternalServerError（HTTP 5xx 才有）。
- 修复前：chat_worker._make_api_call 的 is_server_overload 只匹配 "2064"/
  "overload"，该错误直接 raise，不重试，UI 显示 "[API错误] ..."。
- 修复后：
  1. chat_worker：消息含 5xx 状态码或队列满/流式失败信号 → 按 ServerOverload 重试
  2. ErrorClassifier：无状态码可提取时，按 OVERLOADED_PATTERNS 分类为 overloaded（可重试）
"""

import pytest
from openai import APIError

import httpx

from openai import APIError

from app.core.workers.chat_worker import OpenAIChatWorker
from app.core.workers.error_handler.error_classifier import (
    ErrorClassifier,
    FailoverReason,
    get_error_classifier,
)
from app.core.workers.error_handler.retry_helper import is_retriable_error


# ============================================================================
# chat_worker._make_api_call 真实驱动测试
# ============================================================================


class _SignalStub:
    """替代 PyQt 信号的轻量桩，仅记录 emit 调用。"""

    def __init__(self):
        self.emitted = []

    def emit(self, *args, **kwargs):
        self.emitted.append(args)


class _FakeClient:
    """前 N 次调用抛指定错误，之后成功。记录调用次数。"""

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
            raise _make_api_error(self.error_message)
        return "ok"


def _make_api_error(message: str) -> APIError:
    """构造带指定消息的 openai APIError（模拟 SSE 流内错误事件透传）。"""
    return APIError(message=message, request=httpx.Request("POST", "https://api.example/v1"), body=None)


def _make_worker(fake_client, monkeypatch):
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
    worker.session_id = None
    worker.tools = None
    worker._is_cancelled = False
    worker.retry_resolved = _SignalStub()
    worker.retry_status = _SignalStub()
    worker._http_client = None
    worker._build_api_request_kwargs = lambda: {"model": "gpt-4o", "stream": True}
    worker._get_http_client = lambda: fake_client
    worker._process_response = lambda resp: (True, True)
    # 加速：重试等待不真睡（真实逻辑中每 0.5s 检查一次取消标志）
    monkeypatch.setattr("app.core.workers.chat_worker.time.sleep", lambda s: None)
    return worker


def test_make_api_call_retries_on_streaming_503_queue_full(monkeypatch):
    """核心场景：SSE 流内 503「请求队列已满」→ 自动重试并成功。"""
    client = _FakeClient("Streaming response failed: [503] The request queue is full.")
    worker = _make_worker(client, monkeypatch)

    result = worker._make_api_call([{"role": "user", "content": "hi"}])

    assert client.calls == 2, f"应重试 1 次后成功，实际调用 {client.calls} 次"
    assert result == (True, True)
    # 重试原因应上报为 ServerOverload
    retry_statuses = worker.retry_status.emitted
    assert retry_statuses and retry_statuses[0][0] == "ServerOverload", (
        f"重试原因应为 ServerOverload，实际 {retry_statuses}"
    )


def test_make_api_call_retries_on_queue_full_without_status_code(monkeypatch):
    """无状态码但含「队列已满」信号 → 同样重试（部分服务端不带 [503] 前缀）。"""
    client = _FakeClient("The request queue is full. Please try again later.")
    worker = _make_worker(client, monkeypatch)

    result = worker._make_api_call([{"role": "user", "content": "hi"}])

    assert client.calls == 2, f"应重试 1 次后成功，实际调用 {client.calls} 次"
    assert result == (True, True)


def test_make_api_call_retries_on_generic_502(monkeypatch):
    """消息含 502 状态码（Bad Gateway）→ 同样按过载重试。"""
    client = _FakeClient("Upstream error: [502] Bad Gateway")
    worker = _make_worker(client, monkeypatch)

    result = worker._make_api_call([{"role": "user", "content": "hi"}])

    assert client.calls == 2
    assert result == (True, True)


def test_make_api_call_does_not_retry_auth_error(monkeypatch):
    """非过载错误（认证失败）不应被扩大重试范围。"""
    client = _FakeClient("Invalid API key provided")
    worker = _make_worker(client, monkeypatch)

    with pytest.raises(APIError):
        worker._make_api_call([{"role": "user", "content": "hi"}])

    assert client.calls == 1, "认证错误不应重试"


# ============================================================================
# ErrorClassifier 分类测试
# ============================================================================


def test_classifier_streaming_503_queue_full_is_overloaded():
    """ErrorClassifier：SSE 流内 503 队列满 → overloaded，可重试。"""
    classifier = ErrorClassifier(quiet=True)
    err = _make_api_error("Streaming response failed: [503] The request queue is full.")

    classified = classifier.classify(err)

    assert classified.reason == FailoverReason.overloaded, classified.reason
    assert classified.retryable is True
    assert classified.should_retry is True
    assert classified.should_compress is False


def test_classifier_queue_full_without_status_code_is_overloaded():
    """ErrorClassifier：无状态码的队列满消息 → 走 OVERLOADED_PATTERNS → overloaded。"""
    classifier = ErrorClassifier(quiet=True)
    err = _make_api_error("The request queue is full")

    classified = classifier.classify(err)

    assert classified.reason == FailoverReason.overloaded, classified.reason
    assert classified.should_retry is True


def test_is_retriable_error_true_for_streaming_503():
    """retry_helper.is_retriable_error：对 SSE 流内 503 队列满错误返回 True。"""
    err = _make_api_error("Streaming response failed: [503] The request queue is full.")

    assert is_retriable_error(err) is True


def test_get_error_classifier_shared_instance():
    """全局分类器单例可用（与生产代码同一实例）。"""
    classifier = get_error_classifier()
    err = _make_api_error("Streaming response failed: [503] The request queue is full.")

    classified = classifier.classify(err)

    assert classified.should_retry is True
