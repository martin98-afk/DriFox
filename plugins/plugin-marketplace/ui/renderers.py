# -*- coding: utf-8 -*-
"""内容块渲染器 — 插件市场网格和详情卡"""

from html import escape
from typing import Any, Dict


def _plugin_card_html(plugin: Dict[str, Any], font_size: int = 14) -> str:
    """生成单个插件的 HTML 卡片

    Args:
        plugin: 插件元数据 dict
        font_size: 上下文字体大小（用于派生各元素字号，默认14）
    """
    name = escape(plugin.get("name", ""))
    description = escape(plugin.get("description", ""))
    version = escape(plugin.get("version", ""))
    author = escape(plugin.get("author", ""))
    license_ = escape(plugin.get("license", ""))
    categories = plugin.get("categories", []) or []
    keywords = plugin.get("keywords", []) or []
    homepage = plugin.get("homepage", "")

    # 根据 font_size 派生各元素字号
    fs_name = font_size + 2  # 标题：16px（ctx=14时）
    fs_desc = font_size - 1  # 描述：13px
    fs_small = font_size - 2  # 版本/元信息/按钮：12px
    fs_tiny = font_size - 3  # tag/keyword：11px

    cat_tags = " ".join(f'<span class="tag">{escape(c)}</span>' for c in categories)
    comp_tags = ""
    components = plugin.get("components", {}) or {}
    comp_tags = " ".join(f'<span class="comp">{escape(k)}</span>' for k, v in components.items() if v)
    kw_html = " ".join(f'<span class="keyword">{escape(k)}</span>' for k in keywords[:6])
    homepage_html = f'<a href="{escape(homepage)}" target="_blank" class="homepage">🔗 主页</a>' if homepage else ""

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
    <style>
        .marketplace-card__name {{ font-size: {fs_name}px; }}
        .marketplace-card__version {{ font-size: {fs_small}px; }}
        .marketplace-card__description {{ font-size: {fs_desc}px; }}
        .marketplace-card__meta {{ font-size: {fs_small}px; }}
        .marketplace-card__tags .tag,
        .marketplace-card__tags .comp {{ font-size: {fs_tiny}px; }}
        .marketplace-card__keywords {{ font-size: {fs_tiny}px; }}
        .marketplace-card__install,
        .marketplace-card__actions .homepage {{ font-size: {fs_small}px; }}
    </style>
    """


def render_plugin_grid(data: Dict[str, Any], context) -> str:
    """渲染插件市场网格（多列卡片布局）

    Args:
        data: {"category": "agent", "limit": 20} 或 "plugins": [...] 直接传入
        context: 可选上下文（含 font_size）
    """
    from .data import get_marketplace

    # 从上下文提取 font_size（用于派生 HTML 内各元素字号）
    fs = 14
    if context:
        fs = getattr(context, "get", lambda k, d=14: d)(("font_size",), 14)
        if isinstance(fs, dict):
            fs = fs.get("font_size", 14)
        if not isinstance(fs, (int, float)) or fs <= 0:
            fs = 14

    if "plugins" in data:
        plugins = data["plugins"]
    else:
        category = data.get("category")
        limit = data.get("limit", 20)
        plugins = get_marketplace().list_plugins(category)[:limit]

    if not plugins:
        return '<div class="marketplace-empty">没有可显示的插件</div>'

    cards_html = "".join(_plugin_card_html(p, font_size=fs) for p in plugins)

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
        .marketplace-card__description {{
            color: #ccc;
            line-height: 1.5;
            margin: 8px 0;
        }}
        .marketplace-card__meta {{
            color: #888;
            display: flex;
            gap: 12px;
        }}
        .marketplace-card__tags {{
            margin: 8px 0;
        }}
        .marketplace-card__tags .tag {{
            display: inline-block;
            padding: 2px 8px;
            margin: 2px;
            border-radius: 4px;
            background: rgba(0, 188, 212, 0.15);
            color: #00BCD4;
        }}
        .marketplace-card__tags .comp {{
            display: inline-block;
            padding: 2px 8px;
            margin: 2px;
            border-radius: 4px;
            background: rgba(255, 165, 0, 0.15);
            color: #FFA500;
        }}
        .marketplace-card__keywords {{
            color: #666;
        }}
        .marketplace-card__actions {{
            display: flex;
            gap: 8px;
            margin-top: 10px;
        }}
        .marketplace-card__install {{
            padding: 4px 10px;
            border-radius: 4px;
            background: rgba(76, 175, 80, 0.2);
            color: #4CAF50;
            cursor: pointer;
            text-decoration: none;
            border: none;
        }}
        .marketplace-card__actions .homepage {{
            padding: 4px 10px;
            border-radius: 4px;
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
    """渲染单个插件详情卡

    Args:
        data: 插件数据 dict
        context: 可选上下文（含 font_size）
    """
    plugin = data.get("plugin") or data
    fs = 14
    if context:
        fs = getattr(context, "get", lambda k, d=14: d)(("font_size",), 14)
        if isinstance(fs, dict):
            fs = fs.get("font_size", 14)
        if not isinstance(fs, (int, float)) or fs <= 0:
            fs = 14
    return _plugin_card_html(plugin, font_size=fs)
