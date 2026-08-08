# -*- coding: utf-8 -*-
"""shortcut-manager UI 组件入口"""

import sys
from pathlib import Path

from loguru import logger


def register_ui(registry):
    """注册 shortcut-manager 的 UI 组件"""
    # 清理旧子模块缓存（热重载兼容）
    safe_name = "shortcut_manager"
    prefix = f"ui_plugin_{safe_name}."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    from .cards import ShortcutManagerCard

    registry.register_floating_card(
        plugin_name="shortcut-manager",
        card_id="shortcut-manager",
        widget_class=ShortcutManagerCard,
        container="right",
        title="快捷键管理",
        default_visible=False,
    )

    logger.info("[shortcut-manager] UI components registered")
