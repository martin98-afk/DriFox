# -*- coding: utf-8 -*-
"""
[兼容重导出] AutoLoop 配置 — 已迁移至 app/core/engines/auto_loop/config.py

保持向后兼容，所有原有 import 语句继续可用。
"""
from app.core.engines.auto_loop.config import AutoLoopConfig  # noqa: F401

__all__ = ["AutoLoopConfig"]
