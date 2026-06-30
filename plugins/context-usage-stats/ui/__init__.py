# -*- coding: utf-8 -*-
"""context-usage-stats UI 组件入口"""

import sys

from loguru import logger


def register_ui(registry):
    """注册 context-usage-stats 的 UI 组件

    热重载兼容：
    清理 sys.modules 中残留的子模块缓存，确保 Python 重新从 .py 源文件编译，
    避免旧的 __pycache__/.pyc 导致 NameError 等异常。

    注意：不主动删除 __pycache__/ 目录，Python 的 import 系统已通过
    源文件时间戳自动判断是否需要重新编译 .pyc，主动删除只会触发不必要的
    文件系统变更，导致插件热更新监视器误判为跨插件修改。
    """
    # 清理旧子模块缓存（避免热重载时 Python 用旧 sys.modules 缓存）
    prefix = "ui_plugin_context_usage_stats."
    stale = [k for k in sys.modules if k.startswith(prefix)]
    for k in stale:
        del sys.modules[k]

    from .cards import ContextUsageStatsCard

    # 注册浮动卡片（自动注册对应命令 /context-usage-stats）
    # container="bottom"：与系统配置卡片一致，显示在 chat_layout 下方并隐藏输入区
    registry.register_floating_card(
        plugin_name="context-usage-stats",
        card_id="context-usage-stats",
        widget_class=ContextUsageStatsCard,
        container="bottom",
        title="上下文用量统计",
        default_visible=False,
    )
    logger.info("[context-usage-stats] UI components registered")
