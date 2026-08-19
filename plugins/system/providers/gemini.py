# -*- coding: utf-8 -*-
"""
服务商插件 — Google Gemini

数据全部由本插件声明（万物为插件）：icon / API URL / 默认参数 / 模型列表 /
models.dev 映射 / family 能力。
"""

from app.plugins.registries.provider_registry import ProviderDef


def register(registry):
    """注册 Google Gemini 服务商定义"""
    registry.register(
        ProviderDef(
            name="Google Gemini",
            icon="gemini-ai",
            api_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            auth_type="bearer",
            default_model="gemini-2.0-flash",
            default_params={
                "温度": 0.7,
                "最大Token": 200000,
            },
            register_url="https://aistudio.google.com/app/apikey",
            models=[
                "gemini-2.5-pro-preview-06-05",
                "gemini-2.0-flash",
                "gemini-1.5-pro",
                "gemini-1.5-flash",
                "gemini-1.5-flash-8b",
            ],
            models_dev_id="google",
            family="gemini",
            capabilities={
                "context_limit": 1000000,
                "max_output_tokens": 8192,
                "absolute_limit": 65536,
                "supports_vision": True,
                "supports_thinking": True,
                "thinking_param": "thinking_budget",
            },
        )
    )