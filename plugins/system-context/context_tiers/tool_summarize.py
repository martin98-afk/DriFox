# -*- coding: utf-8 -*-
"""内置 tier 注册薄壳（order 60 旧工具输出摘要化）。"""

from __future__ import annotations

from app.core.context.tiers.tool_summarize import ToolSummarizeTier


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。"""
    registry.register(ToolSummarizeTier())
