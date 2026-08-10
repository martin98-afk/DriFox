# -*- coding: utf-8 -*-
"""plugin-marketplace UI 组件入口"""

from loguru import logger


def register_ui(registry):
    """注册 plugin-marketplace 的 UI 组件"""
    from .cards import MarketplaceCard

    # 以下两条内容块渲染器注册为「无生产端」死注册（P1-6）：
    # 全仓库无代码构造 plugin_marketplace_grid / plugin_marketplace_card 类型的
    # 内容块，启用会走主线程同步 httpx 拉取（render_plugins 内 get_marketplace()）。
    # 勿启用；函数体 render_plugin_grid/render_plugin_card 保留作参考。
    # registry.register_content_renderer(
    #     plugin_name="plugin-marketplace",
    #     type_name="plugin_marketplace_grid",
    #     render_func=render_plugin_grid,
    #     priority=10,
    #     metadata={"description": "插件市场网格"},
    # )
    # registry.register_content_renderer(
    #     plugin_name="plugin-marketplace",
    #     type_name="plugin_marketplace_card",
    #     render_func=render_plugin_card,
    #     priority=10,
    #     metadata={"description": "单个插件详情卡"},
    # )

    # 注册浮动卡片（自动注册对应命令 /plugin-marketplace）
    # container="full"：完整覆盖对话区（与系统配置卡片一致，走覆盖层）
    registry.register_floating_card(
        plugin_name="plugin-marketplace",
        card_id="plugin-marketplace",
        widget_class=MarketplaceCard,
        container="full",
        title="插件市场",
        default_visible=False,
    )
    logger.info("[plugin-marketplace] UI components registered")
