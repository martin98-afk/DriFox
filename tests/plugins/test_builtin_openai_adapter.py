# -*- coding: utf-8 -*-
"""OpenAIAdapter 与 chat_worker 旧协议检测方法行为逐点等价"""

import pytest

from app.plugins.builtin_runtime import OpenAIAdapter, ensure_builtin_adapters
from app.plugins.contracts.model_adapter import ProtocolFlags
from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry


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
    adapter = OpenAIAdapter()
    assert adapter.protocol_flags(llm_config) == _worker_flags(llm_config), f"不等价: {llm_config}"


def test_matches_always_positive():
    assert OpenAIAdapter().matches({}) >= 1


def test_ensure_idempotent_registration():
    ensure_builtin_adapters()
    ensure_builtin_adapters()  # 二次调用不报错不重复
    reg = ModelAdapterRegistry.get_instance()
    assert "openai" in reg.adapters()
