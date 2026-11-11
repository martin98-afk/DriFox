# -*- coding: utf-8 -*-
"""401 自愈回归：OAuth access_token 被服务端提前吊销时换新凭据并重试一次。

背景（2026-11-08 实测）：OpenCode device/token 返回 expires_in=2592000（30 天），
但整条会话链会被服务端提前作废（access 401 + refresh invalid_grant）。修复前
401 直接冒泡给用户，只能手动重新登录。修复后执行层回调服务商插件声明的
``auth_error_hook``，换到新 token 后重试一次；刷新失败按原 401 抛出。
"""

import httpx
import pytest

from app.core.workers.chat_worker import OpenAIChatWorker
from tests.core.protocol_test_helpers import FakeChatTransport, FakeStreamSink


class _SignalStub:
    def __init__(self):
        self.emitted = []

    def emit(self, *args, **kwargs):
        self.emitted.append(args)


class _FakeClient:
    """按预设异常序列抛错，之后成功；记录 create 调用次数。"""

    def __init__(self, errors):
        self._errors = list(errors)
        self.calls = 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return "ok"


def _make_401(message: str = "Invalid credential"):
    from openai import AuthenticationError

    return AuthenticationError(
        message,
        response=httpx.Response(401, request=httpx.Request("POST", "https://api.example/v1/chat/completions")),
        body=None,
    )


def _make_worker(fake_client, monkeypatch):
    """最小可用 worker（绕过 __init__），经 adapter flags 直挂 fake transport。"""
    from types import SimpleNamespace

    worker = OpenAIChatWorker.__new__(OpenAIChatWorker)
    worker.llm_config = {"API_KEY": "rt-x", "API_URL": "https://api.example/v1", "模型名称": "gpt-4o"}
    worker._supports_vision = False
    worker._api_messages_cache = None
    worker._api_messages_built = False
    worker._current_response = None
    worker._partial_content_backup = None
    worker.session_id = None
    worker.tools = None
    worker._is_cancelled = False
    worker._model_adapter = None
    worker.retry_resolved = _SignalStub()
    worker.retry_status = _SignalStub()
    worker._http_client = None
    fake_transport = FakeChatTransport(fake_client)
    # 注入点优先于注册表：单独跑本文件时注册表可能尚未加载系统插件
    worker._chat_transport = fake_transport

    class _FlagsStub:
        extra = {"transport": fake_transport}
        serializer_id = "openai"

        def __getattr__(self, name):
            return None

    monkeypatch.setattr(worker, "_adapter_flags", lambda: _FlagsStub())
    monkeypatch.setattr(
        worker,
        "_serialize_for_api",
        lambda messages, supports_vision=True: SimpleNamespace(messages=list(messages)),
    )
    monkeypatch.setattr(
        worker,
        "_get_stream_sink",
        lambda transport=None: FakeStreamSink(result=(True, True)),
    )
    monkeypatch.setattr("app.core.workers.chat_worker.time.sleep", lambda s: None)
    return worker, fake_transport


def _patch_hook(monkeypatch, result: bool, counter: dict):
    """注入 resolve_auth_error_hook：返回固定结果的钩子并计数。"""

    def _hook(llm_config):
        counter["n"] += 1
        return result

    monkeypatch.setattr(
        "app.core.modelmeta.provider_profile.resolve_auth_error_hook",
        lambda llm_config: _hook,
    )


def test_401_refreshes_and_retries_successfully(monkeypatch):
    """401 → 钩子刷新成功 → 重建认证头重试一次并成功。"""
    client = _FakeClient([_make_401()])
    worker, _ = _make_worker(client, monkeypatch)
    counter = {"n": 0}
    _patch_hook(monkeypatch, True, counter)

    result = worker._make_api_call([{"role": "user", "content": "hi"}])
    assert result == (True, True)
    assert client.calls == 2, "401 后应重试一次"
    assert counter["n"] == 1, "钩子只应调用一次"


def test_401_without_self_heal_raises(monkeypatch):
    """钩子返回 False（链已废 / 无可用凭据）→ 按原 401 抛出，不消耗重试。"""
    client = _FakeClient([_make_401()])
    worker, _ = _make_worker(client, monkeypatch)
    counter = {"n": 0}
    _patch_hook(monkeypatch, False, counter)

    with pytest.raises(Exception) as ei:
        worker._make_api_call([{"role": "user", "content": "hi"}])
    assert type(ei.value).__name__ == "AuthenticationError"
    assert client.calls == 1, "不可自愈时不应重试"
    assert counter["n"] == 1


def test_401_self_heal_only_once(monkeypatch):
    """刷新后仍 401：不再重复刷新，第二次 401 直接抛出。"""
    client = _FakeClient([_make_401(), _make_401()])
    worker, _ = _make_worker(client, monkeypatch)
    counter = {"n": 0}
    _patch_hook(monkeypatch, True, counter)

    with pytest.raises(Exception) as ei:
        worker._make_api_call([{"role": "user", "content": "hi"}])
    assert type(ei.value).__name__ == "AuthenticationError"
    assert client.calls == 2, "自愈只做一次，第二次 401 不再重试"
    assert counter["n"] == 1, "刷新钩子不应被重复调用"


def test_no_hook_provider_does_not_retry(monkeypatch):
    """无 auth_error_hook 的服务商（API Key 版）：401 直接抛出，不重试。"""
    client = _FakeClient([_make_401()])
    worker, _ = _make_worker(client, monkeypatch)
    monkeypatch.setattr(
        "app.core.modelmeta.provider_profile.resolve_auth_error_hook",
        lambda llm_config: None,
    )

    with pytest.raises(Exception) as ei:
        worker._make_api_call([{"role": "user", "content": "hi"}])
    assert type(ei.value).__name__ == "AuthenticationError"
    assert client.calls == 1
