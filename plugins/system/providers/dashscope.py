# -*- coding: utf-8 -*-
"""
服务商插件 — 阿里云 (DashScope)

数据全部由本插件声明（万物为插件）：icon / API URL / 默认参数 / 模型列表 /
models.dev 映射 / family 能力。
"""

from app.plugins.registries.provider_registry import ProviderDef


def register(registry):
    """注册 阿里云 (DashScope) 服务商定义"""
    registry.register(
        ProviderDef(
            name="阿里云 (DashScope)",
            icon="qwen",
            api_url="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            auth_type="bearer",
            default_model="qwen3.5-plus",
            default_params={
                "温度": 0.7,
                "最大Token": 200000,
            },
            register_url="https://bailian.console.aliyun.com/cn-beijing?tab=model#/api-key",
            models=[
                "qwen3-max",
                "qwen3-plus",
                "qwen3.5-max",
            ],
            models_dev_id="alibaba",
            family="dashscope",
            capabilities={
                "context_limit": 1000000,
                "max_output_tokens": 8192,
                "absolute_limit": 65536,
                "supports_vision": True,
                "supports_thinking": False,
                "thinking_param": None,
            },
        )
    )