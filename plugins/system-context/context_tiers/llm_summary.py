# -*- coding: utf-8 -*-
"""内置 tier 注册薄壳（order 80 LLM 摘要，仅 send stage）。"""

from __future__ import annotations

from app.core.context.tiers.llm_summary import LlmSummaryTier


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。"""
    registry.register(LlmSummaryTier())
