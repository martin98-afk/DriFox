# -*- coding: utf-8 -*-
"""
服务商插件 — Ollama（本地推理）

数据全部由本插件声明（万物为插件）：icon / API URL / 默认参数 / 模型列表 /
models.dev 映射 / family 能力。
"""

from app.plugins.registries.provider_registry import ProviderDef


def register(registry):
    """注册 Ollama 服务商定义"""
    registry.register(
        ProviderDef(
            name="Ollama",
            icon="Ollama",
            api_url="http://localhost:11434/v1",
            auth_type="none",
            default_model="llama3",
            default_params={
                "API_KEY": "not-needed",
                "温度": 0.7,
                "最大Token": 200000,
            },
            register_url="https://ollama.com",
            models=[
                "llama3",
                "llama3.1",
                "qwen2.5",
                "qwen2.5-coder",
                "mistral",
                "phi3",
            ],
            models_dev_id="ollama-cloud",
            family="ollama",
            capabilities={
                "context_limit": 200000,
                "max_output_tokens": 8192,
                "absolute_limit": 65536,
                "supports_vision": True,
                "supports_thinking": False,
                "thinking_param": None,
            },
        )
    )