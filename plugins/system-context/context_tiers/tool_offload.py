# -*- coding: utf-8 -*-
"""内置 tier 注册薄壳（order 40 长工具结果落盘，仅 ingest stage）。"""

from __future__ import annotations

from app.core.context.tiers.tool_offload import ToolOffloadTier


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。"""
    registry.register(ToolOffloadTier())
