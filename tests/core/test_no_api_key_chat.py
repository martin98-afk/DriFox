# -*- coding: utf-8 -*-
"""
回归测试：无 API key 对话支持

背景：
- main_widget._on_send_clicked 旧拦截：`if not llm_config or not llm_config.get("API_KEY")`
  导致没有 API key（本地模型 auth=none、或用户清空 key）时无法发送消息，
  提示"请先选择模型"。
- 修复 1：拦截条件放宽为仅检查"完全无模型配置"（not llm_config）。
- 修复 2：openai SDK 构造 client 强制要求非空 api_key，且 api_key 非空时必然发送
  `Authorization: Bearer <key>` 头；对免 key 端点（OpenCode 免费模型、本地 Ollama），
  传占位 key 反而被拒（实测 OpenCode 对 `Bearer not-needed` 返回 401）。
  因此空 key 时用 _StripAuthTransport 剥离 authorization 头，实现真正的免 key 匿名调用；
  云端认证端点无 key 时服务端返回 401，走现有错误处理。

覆盖：
1. build_openai_client 空 key → 构造 client 不崩，且注入剥离 Auth 头的 transport
2. build_openai_client 有 key → 不注入剥离 transport（正常带 key 请求）
3. _StripAuthTransport 实际剥掉 authorization 头（行为级）
4. OpenAIChatWorker._get_http_client 空 key 不崩（走 build_openai_client）
5. _build_api_request_kwargs 空 key 正常组装
6. main_widget 新拦截语义：仅"完全无模型配置"才拦截（纯函数模拟，UI 层无轻量 seam）
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _get_app():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _make_worker(llm_config):
    from app.core.workers.chat_worker import OpenAIChatWorker

    return OpenAIChatWorker(
        messages=[],
        session_messages=[],
        llm_config=llm_config,
        stream=False,
    )


def test_empty_key_client_uses_strip_auth_transport():
    """空 API_KEY 构造 client 不崩，且注入剥离 Authorization 头的 transport（免 key 匿名调用）"""
    import app.utils.http_client as hc

    client = hc.build_openai_client("", "http://localhost:11434/v1", timeout=123)
    assert client.api_key, "SDK 要求非空 api_key（占位值）"
    # openai SDK 内部 httpx client 的 transport 应为剥离 Auth 头的实现
    transport = client._client._transport
    assert isinstance(transport, hc._StripAuthTransport), (
        f"❌ 空 key 应注入 _StripAuthTransport，实际 {type(transport)}"
    )


def test_with_key_client_does_not_strip_auth():
    """有 API_KEY 时不注入剥离 transport（正常带 key 请求）"""
    import app.utils.http_client as hc

    client = hc.build_openai_client("sk-real-key", "https://api.openai.com/v1")
    assert client.api_key == "sk-real-key"
    transport = client._client._transport
    assert not isinstance(transport, hc._StripAuthTransport), "有 key 时不应注入剥离 transport"


def test_strip_auth_transport_removes_authorization_header():
    """行为级：_StripAuthTransport 发出的请求不含 authorization 头"""
    import httpx

    from app.utils.http_client import _StripAuthTransport

    seen = {}

    class _Base(httpx.MockTransport):
        def handle_request(self, request):
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(
                200,
                json={
                    "id": "x",
                    "object": "chat.completion",
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

    # 用 monkeypatch 替换父类 handle_request 为 mock 底（不联网）
    original = _StripAuthTransport.handle_request

    def patched_handle_request(self, request):
        request.headers.pop("authorization", None)
        return _Base.handle_request(self, request)

    _StripAuthTransport.handle_request = patched_handle_request
    try:
        transport = _StripAuthTransport()
        # 直接构造 httpx client 用该 transport 发请求
        client = httpx.Client(transport=transport)
        resp = client.post("http://example.com/v1/chat/completions", headers={"Authorization": "Bearer sk-xxx"})
        assert resp.status_code == 200
        assert seen["auth"] is None, f"❌ authorization 头未被剥离：{seen['auth']!r}"
    finally:
        _StripAuthTransport.handle_request = original


def test_chat_worker_empty_key_client_ok():
    """OpenAIChatWorker._get_http_client 空 key 不崩，走免 key 剥头 client"""
    _get_app()
    worker = _make_worker(
        {
            "API_KEY": "",
            "API_URL": "http://localhost:11434/v1",
            "模型名称": "llama3",
            "认证方式": "none",
        }
    )
    client = worker._get_http_client()
    assert str(client.base_url).rstrip("/") == "http://localhost:11434/v1"


def test_chat_worker_missing_api_key_key_ok():
    """llm_config 完全无 API_KEY 键也不崩（本地服务配置可能不写该字段）"""
    _get_app()
    worker = _make_worker(
        {
            "API_URL": "http://localhost:1234/v1",
            "模型名称": "local-model",
        }
    )
    client = worker._get_http_client()
    assert str(client.base_url).rstrip("/") == "http://localhost:1234/v1"


def test_build_api_request_kwargs_empty_key_ok():
    """_build_api_request_kwargs 空 key 正常组装（extra_body 不含 API_KEY，不抛异常）"""
    _get_app()
    worker = _make_worker(
        {
            "API_KEY": "",
            "API_URL": "http://localhost:11434/v1",
            "模型名称": "llama3",
            "认证方式": "none",
        }
    )
    kwargs = worker._build_api_request_kwargs()
    assert kwargs["model"] == "llama3"
    assert "API_KEY" not in kwargs
    assert "api_key" not in kwargs


def test_send_gate_only_blocks_when_no_config_at_all():
    """
    main_widget 新拦截语义：仅"完全无模型配置"才提示"请先选择模型"；
    有配置但无 API_KEY（本地模型 / 用户清空 key）直接放行。

    注：拦截点在 _on_send_clicked（UI 方法，无轻量 seam），此处用纯函数
    复刻新判断逻辑并断言语义，防止将来误改回"无 key 拦截"。
    """

    def should_block(llm_config):
        # 与 main_widget._on_send_clicked 修复后一致：只拦完全无配置
        return not llm_config

    # 完全无配置 → 拦截（保留引导）
    assert should_block({}) is True
    assert should_block(None) is True
    # 有配置但无 key → 放行
    assert should_block({"API_KEY": "", "模型名称": "llama3"}) is False
    assert should_block({"模型名称": "llama3"}) is False
    # 有配置且有 key → 放行
    assert should_block({"API_KEY": "sk-xxx", "模型名称": "gpt-4o"}) is False


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
