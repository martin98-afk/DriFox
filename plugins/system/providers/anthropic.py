# -*- coding: utf-8 -*-
"""
服务商插件 — Anthropic (Claude)

数据全部由本插件声明（万物为插件）：icon / API URL / 默认参数 / 模型列表 /
models.dev 映射 / family 能力。
"""

from app.plugins.registries.provider_registry import ProviderDef


def register(registry):
    """注册 Anthropic (Claude) 服务商定义"""
    registry.register(
        ProviderDef(
            name="Anthropic (Claude)",
            icon="Anthropic",
            api_url="https://api.anthropic.com/v1",
            auth_type="anthropic",
            default_model="claude-sonnet-4-20250514",
            default_params={
                "温度": 0.7,
                "最大Token": 200000,
            },
            register_url="https://console.anthropic.com/settings/keys",
            models=[
                "claude-sonnet-4-20250514",
                "claude-3-5-sonnet-latest",
                "claude-3-5-haiku-latest",
                "claude-3-opus-latest",
                "claude-3-haiku-latest",
            ],
            models_dev_id="anthropic",
            family="anthropic",
            capabilities={
                "context_limit": 200000,
                "max_output_tokens": 8192,
                "absolute_limit": 65536,
                "supports_vision": True,
                "supports_thinking": True,
                "thinking_param": "thinking",
            },
        )
    )