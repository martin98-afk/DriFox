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

    registry.register_workbench_tab(
        plugin_name="share-history",
        page_id="share-history",
        label="分享记录",
        widget_class=ShareHistoryCard,
        metadata={"order_hint": 20},
    )
    logger.info("[share-history] UI components registered")
