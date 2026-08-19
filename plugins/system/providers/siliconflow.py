# -*- coding: utf-8 -*-
"""
服务商插件 — SiliconFlow (硅基流动)

数据全部由本插件声明：icon / API URL / 默认参数 / 模型列表 /
models.dev 映射 / family 能力 / 余额查询 fetcher。
"""

from app.plugins.registries.provider_registry import ProviderDef, make_bearer_balance_fetcher


def register(registry):
    """注册 SiliconFlow (硅基流动) 服务商定义"""
    registry.register(
        ProviderDef(
            name="SiliconFlow (硅基流动)",
            icon="siliconflow",
            api_url="https://api.siliconflow.cn/v1",
            auth_type="bearer",
            default_model="deepseek-ai/DeepSeek-R1",
            default_params={
                "温度": 0.6,
                "最大Token": 200000,
                "思考预算": "medium",
            },
            register_url="https://cloud.siliconflow.cn/account/ak",
            models=[
                "Qwen/Qwen2.5-7B-Instruct",
                "Qwen/Qwen2.5-14B-Instruct",
                "Qwen/Qwen2.5-72B-Instruct",
                "Qwen/Qwen2.5-7B-Instruct-AWQ",
                "THUDM/glm4-9b-chat",
                "meta-llama/Meta-Llama-3.1-70B-Instruct",
                "meta-llama/Meta-Llama-3.1-8B-Instruct",
                "deepseek-ai/DeepSeek-V2-Chat",
                "Qwen/Qwen2-72B-Instruct",
            ],
            models_dev_id="siliconflow",
            family="siliconflow",
            capabilities={
                "context_limit": 131072,
                "max_output_tokens": 16384,
                "absolute_limit": 65536,
                "supports_vision": False,
                "supports_thinking": True,
                "thinking_param": "thinking_budget",
                "reasoning_effort_param": None,
            },
            balance_fetcher=make_bearer_balance_fetcher(
                url="https://api.siliconflow.cn/v1/user/info",
                balance_key="totalBalance",
                currency="¥",
            ),
        )
    )