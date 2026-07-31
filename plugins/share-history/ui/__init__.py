# -*- coding: utf-8 -*-
"""share-history UI 组件入口"""

import sys

from loguru import logger


def register_ui(registry):
    """注册 share-history 的 UI 组件

    热重载兼容：
    清理 sys.modules 中残留的子模块缓存。
    """
    # 清理旧子模块缓存（避免热重载时 Python 用旧 sys.modules 缓存）
    prefix = "ui_plugin_share_history."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    from .cards import ShareHistoryCard

    # 注册浮动卡片（自动注册对应命令 /share-history）
    # container="right"：停靠在 Tab 窗口右侧停靠区，查阅历史不干扰当前对话
    registry.register_floating_card(
        plugin_name="share-history",
        card_id="share-history",
        widget_class=ShareHistoryCard,
        container="right",
        title="分享记录",
        default_visible=False,
    )
    logger.info("[share-history] UI components registered")
