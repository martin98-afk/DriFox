# -*- coding: utf-8 -*-
"""worker 协议检测委托 adapter registry — 高优先级插件 adapter 可覆盖判定"""
import pytest


def test_plugin_adapter_overrides_worker_detection(monkeypatch):
    worker_cls = pytest.importorskip("app.core.workers.chat_worker").OpenAIChatWorker
    from app.plugins.builtin_runtime import ensure_builtin_adapters
    from app.plugins.contracts.model_adapter import ModelAdapter, ProtocolFlags
    from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry

    ensure_builtin_adapters()

    class _ForceGeminiAdapter:
        id = "force-gemini"

        def matches(self, llm_config: dict) -> int:
            return 99  # 高于内置 openai 的 1

        def protocol_flags(self, llm_config: dict) -> ProtocolFlags:
            return ProtocolFlags(is_gemini=True)

    reg = ModelAdapterRegistry.get_instance()
    reg.register(_ForceGeminiAdapter(), source="plugin:test-force")
    try:
        w = worker_cls.__new__(worker_cls)
        w.llm_config = {"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-4o"}
        w._model_adapter = None
        assert w._is_gemini_model() is True  # 插件覆盖生效
        assert w._requires_reasoning_content() is False
    finally:
        reg.unregister_source("plugin:test-force")


def test_worker_defaults_to_builtin(monkeypatch):
    """无插件覆盖时走内置 openai adapter，判定与旧逻辑一致"""
    worker_cls = pytest.importorskip("app.core.workers.chat_worker").OpenAIChatWorker
    from app.plugins.builtin_runtime import ensure_builtin_adapters

    ensure_builtin_adapters()
    w = worker_cls.__new__(worker_cls)
    w.llm_config = {"API_URL": "https://api.deepseek.com/v1", "模型名称": "deepseek-chat", "思考模式": True}
    w._model_adapter = None
    assert w._requires_reasoning_content() is True
    assert w._is_gemini_model() is False


def test_subagent_worker_delegates():
    """subagent_worker 版的检测方法不依赖 self.llm_config，传参走 adapter registry"""
    executor_cls = pytest.importorskip("app.core.workers.subagent_worker").SubAgentExecutor
    from app.plugins.builtin_runtime import ensure_builtin_adapters

    ensure_builtin_adapters()
    e = executor_cls.__new__(executor_cls)
    # subagent 版方法不带 self.llm_config，直接传参
    assert e._requires_reasoning_content(
        {"API_URL": "https://api.deepseek.com/v1", "模型名称": "deepseek-chat", "思考模式": True}
    ) is True
    assert e._requires_reasoning_content(
        {"API_URL": "", "模型名称": "gpt-4o", "思考模式": True}
    ) is False
