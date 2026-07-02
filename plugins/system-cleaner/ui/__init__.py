# -*- coding: utf-8 -*-
"""system-cleaner UI 组件入口"""

import sys

from loguru import logger


def register_ui(registry):
    """注册 system-cleaner 的 UI 组件

    热重载兼容：
    清理 sys.modules 中残留的子模块缓存，确保 Python 重新从 .py 源文件编译，
    避免旧的 __pycache__/.pyc 导致 NameError 等异常。
    """
    # 清理旧子模块缓存（避免热重载时 Python 用旧 sys.modules 缓存）
    prefix = "ui_plugin_system_cleaner."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    from .cards import SystemCleanerCard

    # 注册浮动卡片（自动注册对应命令 /system-cleaner）
    registry.register_floating_card(
        plugin_name="system-cleaner",
        card_id="system-cleaner",
        widget_class=SystemCleanerCard,
        container="bottom",
        title="系统清理",
        default_visible=False,
    )
    logger.info("[system-cleaner] UI components registered")
