# -*- coding: utf-8 -*-
"""内置 tier 注册薄壳（order 50 工具调用参数截断）。"""

from __future__ import annotations

from app.core.context.tiers.args_truncate import ArgsTruncateTier


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。"""
    registry.register(ArgsTruncateTier())
