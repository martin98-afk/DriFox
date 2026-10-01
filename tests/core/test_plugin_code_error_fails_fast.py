# -*- coding: utf-8 -*-
"""G3 回归：插件编程错误快速失败（不再与网络错误混入重试/文案链路）。

背景：
- transport/sink 插件抛 NameError/AttributeError/TypeError 等是插件代码 bug，
  重试永远不会自愈（每次同炸）。
- 修复前：编程错误落入通用「未知异常」路径，用户只看到裸 traceback（请求失败!），
  与网络故障无法区分。
- 修复后：_make_api_call 识别编程错误立即终止重试（不进 15 次退避），
  _handle_error 归因文案明确指向协议插件代码。
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


def _make_worker(fake_client, monkeypatch):
    """最小可用 worker（绕过 __init__），经 adapter flags 通道直挂 fake transport。

    不走 TransportRegistry 注入：真插件 warmup 时序会晚于 fake 注册并覆盖
    （_test_fake source 优先级低于 plugin:system），实测注册表路径在部分
    运行次序下拿回真 openai_chat 发真网络请求。flags 通道零时序依赖。
    """
    from types import SimpleNamespace

    worker = OpenAIChatWorker.__new__(OpenAIChatWorker)
    worker.llm_config = {"API_KEY": "k", "API_URL": "https://api.example/v1", "模型名称": "gpt-4o"}
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
    # flags stub：__getattr__ 兜底（use_responses_api 等字段按需消费，全部给假值）；
    # extra.transport 直挂 fake transport（flags 通道零注册表时序依赖）
    class _FlagsStub:
        extra = {"transport": fake_transport}
        serializer_id = "openai"

        def __getattr__(self, name):
            return None

    monkeypatch.setattr(worker, "_adapter_flags", lambda: _FlagsStub())
    # 序列化入口直通：messages 已是 API 形状（测试不验证序列化），绕开真 serializer
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
    # 加速：任何退避等待不真睡
    monkeypatch.setattr("app.core.workers.chat_worker.time.sleep", lambda s: None)
    return worker, fake_transport


def test_plugin_code_error_fails_fast_no_retry(monkeypatch):
    """NameError（插件代码 bug）→ 立即抛出，create 仅调用 1 次、零重试信号。"""
    client = _FakeClient([NameError("name 'top_level' is not defined")])
    worker, _ = _make_worker(client, monkeypatch)
    with pytest.raises(NameError):
        worker._make_api_call([{"role": "user", "content": "hi"}])
    assert client.calls == 1, f"编程错误应快速失败（调用 1 次），实际 {client.calls} 次"
    assert worker.retry_status.emitted == [], "编程错误不应产生任何重试状态信号"


def test_plugin_code_error_types_all_fail_fast(monkeypatch):
    """编程错误闭集逐一验证：AttributeError/TypeError/KeyError/ImportError 同样快速失败。"""
    for err in (
        AttributeError("x"),
        TypeError("x"),
        KeyError("x"),
        ImportError("x"),
        NotImplementedError("x"),
    ):
        client = _FakeClient([err])
        worker, _ = _make_worker(client, monkeypatch)
        with pytest.raises(type(err)):
            worker._make_api_call([{"role": "user", "content": "hi"}])
        assert client.calls == 1, f"{type(err).__name__} 应快速失败"


def test_network_error_still_retries_regression(monkeypatch):
    """回归锚：网络类错误（httpx.ConnectError ⊂ NetworkError）仍走既有重试并成功。

    说明：说明书原文用内置 ConnectionError 做锚，但 worker 的重试判定基于
    httpx/httpcore 异常体系（内置 ConnectionError 不在其中，本就不重试），
    故用 httpx.ConnectError 才是「网络错误仍重试」语义的真实锚点。
    """
    client = _FakeClient([httpx.ConnectError("connection refused")])
    worker, _ = _make_worker(client, monkeypatch)
    result = worker._make_api_call([{"role": "user", "content": "hi"}])
    assert client.calls == 2, "网络错误应重试 1 次后成功"
    assert result == (True, True)
    assert worker.retry_status.emitted and worker.retry_status.emitted[0][0] == "ConnectionError"
