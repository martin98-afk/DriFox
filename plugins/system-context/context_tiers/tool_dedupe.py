# -*- coding: utf-8 -*-
"""内置 tier 注册薄壳（order 20 重复工具结果去重）。"""

from __future__ import annotations

from app.core.context.tiers.tool_dedupe import ToolDedupeTier


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。"""
    registry.register(ToolDedupeTier())
