# -*- coding: utf-8 -*-
"""冷启动回归测试：worker 在 backend warmup 之前构造时，
resolve 空注册表不应抛 RuntimeError，而应幂等加载系统插件后正确判定协议。

回归背景：Phase A 删除 builtin_runtime 兜底后，注册表为空时
chat_worker._adapter_flags / subagent_worker._requires_reasoning_content
直接抛 RuntimeError（tests/test_reasoning_content_required.py 7 failed）。
"""

import pytest


@pytest.fixture()
def fresh_registry(monkeypatch):
    """每用例独立 registry（绕过单例状态污染，对齐 test_model_adapter_registry）"""
    from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry

    reg = ModelAdapterRegistry()
    monkeypatch.setattr(ModelAdapterRegistry, "get_instance", staticmethod(lambda: reg))
    return reg


def _opencode_deepseek_config() -> dict:
    """opencode.ai 中转 deepseek-v4 系模型的典型配置（思考模式开启）"""
    return {
        "API_URL": "https://opencode.ai/zen/v1",
        "模型名称": "deepseek-v4-flash-free",
        "思考模式": True,
        "思考等级": "medium",
        "认证方式": "bearer",
    }


def _plain_config() -> dict:
    """非 deepseek 非 gemini 的普通配置（默认协议全 False）"""
    return {"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-4o", "认证方式": "bearer"}


class TestChatWorkerColdStart:
    def test_deepseek_resolves_after_cold_start(self, fresh_registry):
        """冷启动（注册表空）→ deepseek 配置判定 requires_reasoning_content=True 且不抛错"""
        from app.core.workers.chat_worker import OpenAIChatWorker

        worker = OpenAIChatWorker(messages=[], session_messages=[], llm_config=_opencode_deepseek_config())
        assert worker._requires_reasoning_content() is True

    def test_plain_model_defaults_false(self, fresh_registry):
        """冷启动 → 普通模型协议开关全 False（openai 默认语义）"""
        from app.core.workers.chat_worker import OpenAIChatWorker

        worker = OpenAIChatWorker(messages=[], session_messages=[], llm_config=_plain_config())
        flags = worker._adapter_flags()
        assert flags.is_gemini is False
        assert flags.requires_reasoning_content is False
        assert flags.use_responses_api is False

    def test_registered_adapter_wins_without_rescan(self, fresh_registry, monkeypatch):
        """注册表已有适配器时不触发 warmup 重扫（幂等防御：adapters() 非空即跳过）"""
        from app.plugins.contracts.model_adapter import ProtocolFlags

        class _FakeAdapter:
            id = "fake"

            def matches(self, llm_config: dict) -> int:
                return 10

            def protocol_flags(self, llm_config: dict) -> ProtocolFlags:
                return ProtocolFlags(requires_reasoning_content=True)

        fresh_registry.register(_FakeAdapter())
        import app.plugins.loaders.runtime_component_loader as loader_mod

        called = []

        def _spy():
            called.append(1)
            return {}

        monkeypatch.setattr(loader_mod, "warmup_runtime_components", _spy)
        from app.core.workers.chat_worker import OpenAIChatWorker

        worker = OpenAIChatWorker(messages=[], session_messages=[], llm_config=_plain_config())
        assert worker._requires_reasoning_content() is True
        assert called == [], "注册表非空时不应触发 warmup 重扫"


class TestSubAgentWorkerColdStart:
    def test_deepseek_resolves_after_cold_start(self, fresh_registry):
        """冷启动 → subagent_worker 对 deepseek 配置判定 True 且不抛错"""
        from app.core.workers.subagent_worker import SubAgentExecutor

        executor = SubAgentExecutor.__new__(SubAgentExecutor)
        assert executor._requires_reasoning_content(_opencode_deepseek_config()) is True

    def test_plain_model_defaults_false(self, fresh_registry):
        """冷启动 → subagent_worker 普通模型判定 False"""
        from app.core.workers.subagent_worker import SubAgentExecutor

        executor = SubAgentExecutor.__new__(SubAgentExecutor)
        assert executor._requires_reasoning_content(_plain_config()) is False

    def test_llm_config_none_safe(self, fresh_registry):
        """llm_config 为 None 时防御（不抛 TypeError）"""
        from app.core.workers.subagent_worker import SubAgentExecutor

        executor = SubAgentExecutor.__new__(SubAgentExecutor)
        assert executor._requires_reasoning_content(None) is False


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
