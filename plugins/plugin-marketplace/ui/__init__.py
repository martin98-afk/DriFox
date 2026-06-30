# -*- coding: utf-8 -*-
"""plugin-marketplace UI 组件入口"""

from loguru import logger


def register_ui(registry):
    """注册 plugin-marketplace 的 UI 组件"""
    from .renderers import render_plugin_grid, render_plugin_card
    from .cards import MarketplaceCard

    # 注册内容块渲染器
    registry.register_content_renderer(
        plugin_name="plugin-marketplace",
        type_name="plugin_marketplace_grid",
        render_func=render_plugin_grid,
        priority=10,
        metadata={"description": "插件市场网格"},
    )
    registry.register_content_renderer(
        plugin_name="plugin-marketplace",
        type_name="plugin_marketplace_card",
        render_func=render_plugin_card,
        priority=10,
        metadata={"description": "单个插件详情卡"},
    )

    # 注册浮动卡片（自动注册对应命令 /plugin-marketplace）
    # container="bottom"：与系统配置卡片一致，显示在 chat_layout 下方并隐藏输入区
    registry.register_floating_card(
        plugin_name="plugin-marketplace",
        card_id="plugin-marketplace",
        widget_class=MarketplaceCard,
        container="bottom",
        title="插件市场",
        default_visible=False,
    )
    logger.info("[plugin-marketplace] UI components registered")
