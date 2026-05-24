# -*- coding: utf-8 -*-
"""
Gateway 工具函数
"""
from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger


def platform_httpx_limits() -> "httpx.Limits":
    """
    获取 httpx 连接限制配置
    
    Returns:
        httpx.Limits
    """
    try:
        import httpx
        return httpx.Limits(
            max_keepalive_connections=20,
            max_connections=100,
            keepalive_expiry=30.0,
        )
    except ImportError:
        logger.warning("[Helpers] httpx not installed")
        return None