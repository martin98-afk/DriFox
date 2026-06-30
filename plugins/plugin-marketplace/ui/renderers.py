# -*- coding: utf-8 -*-
"""内容块渲染器 — 插件市场网格和详情卡"""
from html import escape
from typing import Any, Dict


def _plugin_card_html(plugin: Dict[str, Any]) -> str:
    """生成单个插件的 HTML 卡片"""
    name = escape(plugin.get("name", ""))
    description = escape(plugin.get("description", ""))
    version = escape(plugin.get("version", ""))
    author = escape(plugin.get("author", ""))
    license_ = escape(plugin.get("license", ""))
    categories = plugin.get("categories", []) or []
    keywords = plugin.get("keywords", []) or []
    homepage = plugin.get("homepage", "")

    cat_tags = " ".join(
        f'<span class="tag">{escape(c)}</span>' for c in categories
    )
    comp_tags = ""
    components = plugin.get("components", {}) or {}
    comp_tags = " ".join(
        f'<span class="comp">{escape(k)}</span>'
        for k, v in components.items() if v
    )
    kw_html = " ".join(
        f'<span class="keyword">{escape(k)}</span>'
        for k in keywords[:6]
    )
    homepage_html = (
        f'<a href="{escape(homepage)}" target="_blank" class="homepage">'
        f'🔗 主页</a>' if homepage else ''
    )

    return f"""
    <div class="marketplace-card" data-plugin-name="{name}">
        <div class="marketplace-card__header">
            <h3 class="marketplace-card__name">{name}</h3>
            <span class="marketplace-card__version">v{version}</span>
        </div>
        <p class="marketplace-card__description">{description}</p>
        <div class="marketplace-card__meta">
            <span class="author">👤 {author}</span>
            <span class="license">{license_}</span>
        </div>
        <div class="marketplace-card__tags">
            {cat_tags}
            {comp_tags}
        </div>
        <div class="marketplace-card__keywords">{kw_html}</div>
        <div class="marketplace-card__actions">
            {homepage_html}
            <button class="marketplace-card__install" data-plugin="{name}">
                📥 安装
            </button>
        </div>
    </div>
    """


def render_plugin_grid(data: Dict[str, Any], context) -> str:
    """渲染插件市场网格（多列卡片布局）

    Args:
        data: {"category": "agent", "limit": 20} 或 "plugins": [...] 直接传入
        context: 可选上下文
    """
    from .data import get_marketplace

    if "plugins" in data:
        plugins = data["plugins"]
    else:
        category = data.get("category")
        limit = data.get("limit", 20)
        plugins = get_marketplace().list_plugins(category)[:limit]

    if not plugins:
        return '<div class="marketplace-empty">没有可显示的插件</div>'

    cards_html = "".join(_plugin_card_html(p) for p in plugins)

    return f"""
    <div class="marketplace-grid">
        {cards_html}
    </div>
    <style>
        .marketplace-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 12px;
            margin: 12px 0;
        }}
        .marketplace-card {{
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 10px;
            padding: 14px;
            background: rgba(255, 255, 255, 0.03);
        }}
        .marketplace-card__header {{
            display: flex;
            justify-content: space-between;
            align-items: baseline;
        }}
        .marketplace-card__name {{
            margin: 0;
            font-size: 16px;
            color: #FFA500;
        }}
        .marketplace-card__version {{
            font-size: 12px;
            color: #888;
        }}
        .marketplace-card__description {{
            font-size: 13px;
            color: #ccc;
            line-height: 1.5;
            margin: 8px 0;
        }}
        .marketplace-card__meta {{
            font-size: 12px;
            color: #888;
            display: flex;
            gap: 12px;
        }}
        .marketplace-card__tags {{
            margin: 8px 0;
        }}
        .marketplace-card__tags .tag,
        .marketplace-card__tags .comp {{
            display: inline-block;
            padding: 2px 8px;
            margin: 2px;
            border-radius: 4px;
            font-size: 11px;
            background: rgba(0, 188, 212, 0.15);
            color: #00BCD4;
        }}
        .marketplace-card__tags .comp {{
            background: rgba(255, 165, 0, 0.15);
            color: #FFA500;
        }}
        .marketplace-card__keywords {{
            font-size: 11px;
            color: #666;
        }}
        .marketplace-card__actions {{
            display: flex;
            gap: 8px;
            margin-top: 10px;
        }}
        .marketplace-card__install,
        .marketplace-card__actions .homepage {{
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 12px;
            background: rgba(76, 175, 80, 0.2);
            color: #4CAF50;
            cursor: pointer;
            text-decoration: none;
            border: none;
        }}
        .marketplace-empty {{
            text-align: center;
            color: #888;
            padding: 30px;
        }}
    </style>
    """


def render_plugin_card(data: Dict[str, Any], context) -> str:
    """渲染单个插件详情卡"""
    plugin = data.get("plugin") or data
    return _plugin_card_html(plugin)
