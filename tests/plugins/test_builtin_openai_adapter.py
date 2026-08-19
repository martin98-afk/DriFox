# -*- coding: utf-8 -*-
"""协议家族适配器与 chat_worker 旧协议检测方法行为逐点等价。

实现已从旧单适配器迁入系统插件三家族 plugins/system/model_adapters/：
openai-family（兜底）/ gemini-family / deepseek-family。
"""

import pytest

from app.plugins.contracts.model_adapter import ProtocolFlags


@pytest.fixture(scope="module", autouse=True)
def _warmup_runtime():
    """注册表去兜底后，等价性基准需真实注册系统插件（替代旧的 ensure_builtin_adapters）"""
    from app.plugins.loaders.runtime_component_loader import warmup_runtime_components

    warmup_runtime_components()


def _worker_flags(llm_config: dict):
    """直接调用旧 worker 方法（__new__ 免启动），作为等价性基准"""
    worker_cls = pytest.importorskip("app.core.workers.chat_worker").OpenAIChatWorker
    w = worker_cls.__new__(worker_cls)
    w.llm_config = llm_config
    return ProtocolFlags(
        is_gemini=w._is_gemini_model(),
        requires_reasoning_content=w._requires_reasoning_content(),
        use_responses_api=w._use_responses_api(),
    )


_CASES = [
    {"API_URL": "https://api.deepseek.com/v1", "模型名称": "deepseek-chat", "思考模式": True},
    {"API_URL": "https://opencode.ai/v1", "模型名称": "deepseek-v4", "思考模式": True},
    {"API_URL": "https://generativelanguage.googleapis.com/v1", "模型名称": "gemini-2.5-pro"},
    {"API_URL": "", "模型名称": "models/gemini-3-flash-preview"},
    {"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-4o"},
    {"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-5.6-luna"},
    {"API_URL": "https://api.openai.com/v1", "模型名称": "gpt-4o", "使用ResponsesAPI": True},
    {"API_URL": "https://api.siliconflow.cn/v1", "模型名称": "Qwen/Qwen3-Coder", "思考模式": True},
]


@pytest.mark.parametrize("llm_config", _CASES)
def test_flags_equivalent_to_legacy(llm_config):
    """resolve 选中的家族返回的 flags == 旧 worker 方法（逐点等价）"""
    from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry

    adapter = ModelAdapterRegistry.get_instance().resolve(llm_config)
    assert adapter is not None, f"无家族命中: {llm_config}"
    assert adapter.protocol_flags(llm_config) == _worker_flags(llm_config), f"不等价: {llm_config}"


def test_family_adapters_always_positive():
    """三家族 matches 恒正/条件正：openai-family 恒 1，其余按命中条件"""
    from plugins.system.model_adapters.deepseek_family import DeepSeekFamilyAdapter
    from plugins.system.model_adapters.gemini_family import GeminiFamilyAdapter
    from plugins.system.model_adapters.openai_family import OpenAIFamilyAdapter

    assert OpenAIFamilyAdapter().matches({}) >= 1
    assert GeminiFamilyAdapter().matches({"模型名称": "gemini-2.5-pro"}) >= 1
    assert DeepSeekFamilyAdapter().matches({"模型名称": "deepseek-chat", "思考模式": True}) >= 1


def test_system_plugin_registers_families(monkeypatch):
    """warmup_runtime_components() 后注册表含三家族（替代旧的 ensure_idempotent 断言）"""
    from app.plugins.loaders.runtime_component_loader import warmup_runtime_components
    from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry

    # 隔离全局单例：fresh registry + 替换 get_instance
    reg = ModelAdapterRegistry()
    monkeypatch.setattr(ModelAdapterRegistry, "get_instance", staticmethod(lambda: reg))
    warmup_runtime_components()
    # 注册表含三家族（不再有旧单适配器 openai）
    adapters = reg.adapters()
    assert {"openai-family", "gemini-family", "deepseek-family"} <= set(adapters)
    assert "openai" not in adapters
