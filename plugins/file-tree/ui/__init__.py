# -*- coding: utf-8 -*-
"""file-tree UI 组件入口"""

import sys

from loguru import logger


def register_ui(registry):
    """注册 file-tree 的 UI 组件

    热重载兼容：
    清理 sys.modules 中残留的子模块缓存，确保 Python 重新从 .py 源文件编译，
    避免旧的 __pycache__/.pyc 导致 NameError 等异常。
    """
    # 清理旧子模块缓存（避免热重载时 Python 用旧 sys.modules 缓存）
    prefix = "ui_plugin_file_tree."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    from .cards import FileTreeCard

    # 注册工作台页签（右侧边栏常驻；自动注册对应命令 /file-tree）
    registry.register_workbench_tab(
        plugin_name="file-tree",
        page_id="file-tree",
        label="文件树",
        widget_class=FileTreeCard,
        priority=10,
        metadata={"source": "system", "order_hint": 15},
    )
    logger.info("[file-tree] UI components registered")
