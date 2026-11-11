# -*- coding: utf-8 -*-
"""
服务商插件 — MiniMax 视频生成（海螺视频）

视频生成样例 ProviderDef：与 MiniMax chat 定义（minimax.py，api.minimax.chat）
不同域（api.minimaxi.com），config_id (URL, KEY) 不撞车。
"""

from app.plugins.registries.provider_registry import ProviderDef


def register(registry):
    """注册 海螺视频 服务商定义"""
    registry.register(
        ProviderDef(
            name="海螺视频",
            icon="MiniMax",
            api_url="https://api.minimaxi.com/v1",
            auth_type="bearer",
            default_model="video-01",
            models=["video-01"],
            capability_type="video_generation",
            register_url="https://platform.minimaxi.com",
        )
    )
