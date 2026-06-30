# -*- coding: utf-8 -*-
"""plugin-manager UI 组件入口"""

import shutil
import sys
from pathlib import Path

from loguru import logger


def register_ui(registry):
    """注册 plugin-manager 的 UI 组件

    热重载兼容：
    清理 sys.modules 中残留的子模块缓存，确保 Python 重新从 .py 源文件编译，
    避免旧的 __pycache__/.pyc 导致 NameError 等异常。
    """
    # 清理旧子模块缓存（避免热重载时 Python 用旧 sys.modules 缓存）
    prefix = "ui_plugin_plugin_manager."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    # 清理旧的 __pycache__（确保重新编译）
    pycache = Path(__file__).parent / "__pycache__"
    if pycache.exists():
        shutil.rmtree(pycache, ignore_errors=True)

    from .cards import PluginManagerCard

    # 注册浮动卡片（自动注册对应命令 /plugin-manager）
    registry.register_floating_card(
        plugin_name="plugin-manager",
        card_id="plugin-manager",
        widget_class=PluginManagerCard,
        container="top",
        title="插件管理",
        default_visible=False,
    )
    logger.info("[plugin-manager] UI components registered")
