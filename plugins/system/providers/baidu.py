# -*- coding: utf-8 -*-
"""
服务商插件 — 百度千帆

数据全部由本插件声明（万物为插件）：icon / API URL / 默认参数 / 模型列表 /
models.dev 映射 / family 能力。
"""

from app.plugins.registries.provider_registry import ProviderDef


def register(registry):
    """注册 百度千帆 服务商定义"""
    registry.register(
        ProviderDef(
            name="百度千帆",
            icon="baidu",
            api_url="https://qianfan.baidubce.com/v2",
            auth_type="bce",
            default_model="ernie-3.5-8k",
            default_params={
                "温度": 0.7,
                "最大Token": 200000,
            },
            register_url="https://console.bce.baidu.com/qianfan/ais/console/apikey",
            models=[
                "ernie-3.5-8k",
                "ernie-3.5-4k",
                "ernie-speed-8k",
                "ernie-speed-128k",
            ],
            models_dev_id="",
            family="baidu_qianfan",
            capabilities={
                "token_ratio": 0.55,  # 本地 token 估算校正系数（除数）；百度 ERNIE 中文分词效率略低
                "context_limit": 200000,
                "max_output_tokens": 8192,
                "absolute_limit": 65536,
                "supports_vision": False,
                "supports_thinking": False,
                "thinking_param": None,
            },
        )
    )