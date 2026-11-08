# -*- coding: utf-8 -*-
"""插件市场展示名解析：转调主程序公共契约层，避免两处口径漂移。"""

from app.plugins.contracts.plugin_display import plugin_display_name  # noqa: F401

__all__ = ["plugin_display_name"]
