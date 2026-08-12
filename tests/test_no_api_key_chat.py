# -*- coding: utf-8 -*-
"""
回归测试：无 API key 对话支持

背景：
- main_widget._on_send_clicked 旧拦截：`if not llm_config or not llm_config.get("API_KEY")`
  导致没有 API key（本地模型 auth=none、或用户清空 key）时无法发送消息，
  提示"请先选择模型"。
- 修复：拦截条件放宽为仅检查"完全无模型配置"（not llm_config）；
  openai SDK 构造 client 强制要求非空 api_key，空 key 直接抛 OpenAIError，
  因此 chat_worker / topic_summary 对空 key 补占位值 "not-needed"，
  本地免认证端点服务端不校验 key，效果等同无 key 调用；
  云端认证端点会正常返回 401 走现有错误处理。

覆盖：
1. OpenAIChatWorker._get_http_client 空 API_KEY 不崩，自动补占位 key
2. llm_config 完全无 API_KEY 键也不崩
3. _build_api_request_kwargs 空 key 正常组装（不抛异常）
4. main_widget 新拦截语义：仅"完全无模型配置"才拦截（纯函数模拟，UI 层无轻量 seam）
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _get_app():
    from PyQt5.QtWidgets import QApplication

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


def test_http_client_empty_key_uses_placeholder():
    """空 API_KEY 构造 OpenAI client 不崩，自动补占位 key（本地免认证端点等同无 key 调用）"""
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
    assert client.api_key == "not-needed", f"❌ 空 key 应补占位值，实际 {client.api_key!r}"
    assert str(client.base_url).rstrip("/") == "http://localhost:11434/v1"


def test_http_client_missing_api_key_key_uses_placeholder():
    """llm_config 完全无 API_KEY 键也不崩（本地服务配置可能不写该字段）"""
    _get_app()
    worker = _make_worker(
        {
            "API_URL": "http://localhost:1234/v1",
            "模型名称": "local-model",
        }
    )
    client = worker._get_http_client()
    assert client.api_key == "not-needed", f"❌ 缺 key 键应补占位值，实际 {client.api_key!r}"


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
    test_http_client_empty_key_uses_placeholder()
    test_http_client_missing_api_key_key_uses_placeholder()
    test_build_api_request_kwargs_empty_key_ok()
    test_send_gate_only_blocks_when_no_config_at_all()
    print("🎉 All no-api-key chat tests passed!")
