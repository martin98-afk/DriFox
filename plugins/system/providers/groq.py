# -*- coding: utf-8 -*-
"""
服务商插件 — Groq

数据全部由本插件声明（万物为插件）：icon / API URL / 默认参数 / 模型列表 /
models.dev 映射 / family 能力。
"""

from app.plugins.registries.provider_registry import ProviderDef


def register(registry):
    """注册 Groq 服务商定义"""
    registry.register(
        ProviderDef(
            name="Groq",
            icon="groq",
            api_url="https://api.groq.com/openai/v1",
            auth_type="bearer",
            default_model="llama-3.1-70b-versatile",
            default_params={
                "温度": 0.7,
                "最大Token": 200000,
            },
            register_url="https://console.groq.com/keys",
            models=[
                "openai/gpt-oss-120b",
                "qwen/qwen3-32b",
                "groq/compound",
                "llama-3.3-70b-versatile",
                "meta-llama/llama-4-scout-17b-16e-instruct",
                "moonshotai/kimi-k2-instruct-0905",
            ],
            models_dev_id="groq",
            family="groq",
            capabilities={
                "context_limit": 200000,
                "max_output_tokens": 8192,
                "absolute_limit": 65536,
                "supports_vision": False,
                "supports_thinking": False,
                "thinking_param": None,
            },
        )
    )