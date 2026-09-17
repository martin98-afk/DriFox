# -*- coding: utf-8 -*-
"""内置 tier 注册薄壳（order 10 历史图片剥离）。"""

from __future__ import annotations

from app.core.context.tiers.media_strip import MediaStripTier


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。"""
    registry.register(MediaStripTier())
