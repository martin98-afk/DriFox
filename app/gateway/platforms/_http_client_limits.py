# -*- coding: utf-8 -*-
"""
HTTP 客户端连接限制配置

为平台适配器提供统一的 httpx 连接池配置。
"""
from __future__ import annotations


def platform_httpx_limits():
    """
    获取 httpx 连接限制配置
    
    配置说明：
    - max_keepalive_connections: 保活连接数
    - max_connections: 最大连接数
    - keepalive_expiry: 保活过期时间(秒)
    
    Returns:
        httpx.Limits 或兼容对象
    """
    try:
        import httpx
        return httpx.Limits(
            max_keepalive_connections=20,
            max_connections=100,
            keepalive_expiry=30.0,
        )
    except ImportError:
        # 如果 httpx 未安装，返回 None
        return None
