# -*- coding: utf-8 -*-
"""Phase C E2E 验收：家族覆盖 + serializer_id 指定 + 冷启动（万物即插件判据）。

1. 家族组合：user 根覆盖 deepseek-family → deepseek 配置走自定义家族，gemini/其他不受影响
2. 单入口：adapter 指定 serializer_id="demo" → worker 请求构建走自定义序列化器；卸载回退
3. 冷启动：不跑 backend warmup 直接构造 worker → 单入口 + 家族解析不抛异常
"""

import pytest

from app.plugins.contracts.message_serializer import SerializeContext, SerializeResult
from app.plugins.contracts.model_adapter import ProtocolFlags


@pytest.fixture()
def fresh_adapter_registry(monkeypatch):
    from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry

    reg = ModelAdapterRegistry()
    monkeypatch.setattr(ModelAdapterRegistry, "get_instance", staticmethod(lambda: reg))
    return reg


@pytest.fixture()
def fresh_serializer_registry(monkeypatch):
    from app.plugins.registries.serializer_registry import SerializerRegistry

    reg = SerializerRegistry()
    monkeypatch.setattr(SerializerRegistry, "get_instance", staticmethod(lambda: reg))
    return reg


@pytest.fixture()
def fresh_storage_registry(monkeypatch):
    """隔离 StorageRegistry（warmup 冷启动链路不串扰）"""
    from app.plugins.registries.storage_registry import StorageRegistry

    reg = StorageRegistry()
    monkeypatch.setattr(StorageRegistry, "get_instance", staticmethod(lambda: reg))
    return reg


# ---------- Step 1: 家族覆盖 E2E ----------


def test_family_override_e2e(fresh_adapter_registry, fresh_serializer_registry, fresh_storage_registry):
    """user 根覆盖 deepseek-family → deepseek 配置走自定义家族；gemini/其他不受影响"""
    from app.plugins.loaders.runtime_component_loader import warmup_runtime_components
    from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry

    warmup_runtime_components()
    reg = ModelAdapterRegistry.get_instance()

    deepseek_cfg = {"API_URL": "https://api.deepseek.com/v1", "模型名称": "deepseek-chat", "思考模式": True}
    gemini_cfg = {"API_URL": "https://generativelanguage.googleapis.com/v1", "模型名称": "gemini-2.5-pro"}
    plain_cfg = {"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-4o"}

    # 基线：系统 deepseek-family 生效
    assert reg.resolve(deepseek_cfg).id == "deepseek-family"

    class _CustomDeepSeekFamily:
        """user 根覆盖：deepseek 配置 → 强制 requires_reasoning_content=False（语义反转）"""

        id = "deepseek-family"

        def matches(self, llm_config):
            # 保持与原家族同源条件（仅 deepseek 命中），避免抢占其他家族
            from plugins.system.model_adapters import _detectors as det

            return 3 if det.detect_requires_reasoning(llm_config) else 0

        def protocol_flags(self, llm_config):
            return ProtocolFlags(requires_reasoning_content=False)

    reg.register(_CustomDeepSeekFamily(), source="plugin:demo")
    assert reg.resolve(deepseek_cfg).id == "deepseek-family"
    assert reg.resolve(deepseek_cfg).protocol_flags(deepseek_cfg).requires_reasoning_content is False
    # gemini / 其他不受影响（家族正交）
    assert reg.resolve(gemini_cfg).id == "gemini-family"
    assert reg.resolve(plain_cfg).id == "openai-family"

    # 卸载 + 重扫（热重载语义）→ 回退系统 deepseek-family
    reg.unregister_source("plugin:demo")
    warmup_runtime_components()  # scan_roots 幂等重扫，恢复系统三家族
    assert reg.resolve(deepseek_cfg).protocol_flags(deepseek_cfg).requires_reasoning_content is True


# ---------- Step 2: serializer_id 指定 E2E ----------


def test_serializer_id_flow_e2e(fresh_adapter_registry, fresh_serializer_registry, fresh_storage_registry):
    """adapter 指定 serializer_id="demo" → worker 构建走自定义序列化器；卸载回退 openai"""
    from app.plugins.loaders.runtime_component_loader import warmup_runtime_components

    warmup_runtime_components()  # 系统默认（openai-family + openai serializer）

    class _DemoSerializer:
        id = "demo"

        def serialize(self, messages, ctx: SerializeContext):
            return SerializeResult(messages=[{"role": "user", "content": "DEMO-SER"}])

    class _DemoAdapter:
        """user 根 demo-family：普通 openai 配置也指定 demo 序列化器"""

        id = "demo-family"

        def matches(self, llm_config):
            return 5

        def protocol_flags(self, llm_config):
            return ProtocolFlags(serializer_id="demo")

    from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry
    from app.plugins.registries.serializer_registry import SerializerRegistry

    ad_reg = ModelAdapterRegistry.get_instance()
    ser_reg = SerializerRegistry.get_instance()
    ad_reg.register(_DemoAdapter(), source="plugin:demo")
    ser_reg.register(_DemoSerializer(), source="plugin:demo")

    from app.core.workers.chat_worker import OpenAIChatWorker

    worker = OpenAIChatWorker(
        messages=[{"role": "user", "content": "hi"}],
        session_messages=[],
        llm_config={"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-4o"},
    )
    assert worker._build_api_messages_cache() == [{"role": "user", "content": "DEMO-SER"}]

    # 卸载 → 回退系统默认（openai serializer）
    ad_reg.unregister_source("plugin:demo")
    ser_reg.unregister_source("plugin:demo")
    worker2 = OpenAIChatWorker(
        messages=[{"role": "user", "content": "hi"}],
        session_messages=[],
        llm_config={"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-4o"},
    )
    assert worker2._build_api_messages_cache() == [{"role": "user", "content": "hi"}]


# ---------- Step 3: 冷启动 E2E ----------


def test_cold_start_worker_e2e(fresh_adapter_registry, fresh_serializer_registry, fresh_storage_registry):
    """不跑 warmup 直接构造 worker：单入口 + 家族解析均不抛异常（幂等加载防御）"""
    from app.core.workers.chat_worker import OpenAIChatWorker
    from app.core.workers.subagent_worker import SubAgentExecutor

    # chat_worker：家族解析 + 单入口构建
    worker = OpenAIChatWorker(
        messages=[{"role": "user", "content": "hi"}],
        session_messages=[],
        llm_config={"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-4o"},
    )
    assert worker._build_api_messages_cache() == [{"role": "user", "content": "hi"}]
    flags = worker._adapter_flags()
    assert flags.serializer_id == "openai"
    assert flags.is_gemini is False

    # subagent：responses 单入口（deepseek 配置走家族解析）
    executor = SubAgentExecutor.__new__(SubAgentExecutor)
    deepseek_cfg = {"API_URL": "https://api.deepseek.com/v1", "模型名称": "deepseek-chat", "思考模式": True}
    result = executor._serialize_for_api([{"role": "user", "content": "hi"}], deepseek_cfg)
    assert result.messages == [{"role": "user", "content": "hi"}]


def test_kernel_probes_phase_c_components():
    """kernel 组件探测：serializers 已登记；model_adapters 家族文件可被扫描"""
    from app.plugins.kernel import KNOWN_COMPONENTS

    assert "serializers" in KNOWN_COMPONENTS
    assert "model_adapters" in KNOWN_COMPONENTS


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
