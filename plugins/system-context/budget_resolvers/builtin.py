# -*- coding: utf-8 -*-
"""内置预算解析器注册（id="builtin"）。"""

from __future__ import annotations

from app.core.context.budget import BuiltInBudgetResolver


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。"""
    registry.register(BuiltInBudgetResolver())
