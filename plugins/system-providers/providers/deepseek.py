# -*- coding: utf-8 -*-
"""
服务商插件 — DeepSeek

数据全部由本插件声明（万物为插件）：icon / API URL / 默认参数 / 模型列表 /
models.dev 映射 / family 能力 / 余额查询 fetcher。
"""

from app.plugins.registries.provider_registry import ProviderDef, make_bearer_balance_fetcher


def register(registry):
    """注册 DeepSeek 服务商定义"""
    registry.register(
        ProviderDef(
            name="DeepSeek",
            icon="deepseek",
            api_url="https://api.deepseek.com",
            auth_type="bearer",
            default_model="deepseek-chat",
            default_params={
                "温度": 0.7,
                "最大Token": 200000,
                "思考模式": False,
                "思考等级": "high",
            },
            register_url="https://platform.deepseek.com/api_keys",
            models=[
                "deepseek-v4-flash",
                "deepseek-v4-pro",
            ],
            models_dev_id="deepseek",
            family="deepseek",
            capabilities={
                "token_ratio": 1.00,  # 本地 token 估算校正系数（除数）；cl100k_base 基线经 OpenCode 验证已准确，统一 1.0；见 token_estimator._MODEL_TOKEN_RATIOS
                "context_limit": 320000,
                "max_output_tokens": 8192,
                "absolute_limit": 65536,
                "supports_vision": False,
                "supports_thinking": True,
                "thinking_param": "thinking",
                "reasoning_effort_param": "reasoning_effort",
            },
            balance_fetcher=make_bearer_balance_fetcher(
                url="https://api.deepseek.com/user/balance",
                balance_key="total_balance",
                currency="¥",
            ),
        )
    )