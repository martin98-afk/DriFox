# -*- coding: utf-8 -*-
"""欢迎卡片「更新」tab — 渲染逻辑 + CSS/JS 内嵌。

register_welcome_tab 的 render_func 入口 ``render_changelog(ctx)`` 返回完整 HTML
片段（含 ``<style>`` / 列表 / ``<script>``），由主程序 markdown 管线拼到 viewer。

状态机（与 ``_fetcher`` 协同）：
- 缓存空 → 返回 loading 占位 + 触发 ``start_fetch()``（幂等）
- 缓存有 last_error → 返回错误占位
- 缓存命中 → 渲染版本列表
- fetcher 完成 → 写缓存 + 派发 ``EV_WELCOME_TAB_REFRESHED`` → 卡片重渲 → 命中缓存
"""

from __future__ import annotations

from html import escape
from typing import Any, Dict

from app.utils.design_tokens import scale_font_size

from . import _fetcher

_TAG_FONT_PX = scale_font_size(12)
_TINY_FONT_PX = scale_font_size(10)


def render_changelog(ctx: Dict[str, Any]) -> str:
    """render_func 入口（被 UIPluginRegistry 调用，ctx 由主程序注入主题/窗口信息）

    签名要求：``(context: dict) -> str(HTML 片段)``
    """
    state = _fetcher.get_cached_state()
    releases = state["releases"]
    last_error = state.get("last_error")
    loading = state.get("loading")

    if last_error:
        return _render_error(last_error)
    if not releases:
        # 缓存未命中 → 触发拉取（幂等：缓存有效会立即派发刷新，这里进不到）
        if not loading:
            _fetcher.start_fetch()
        return _render_loading()
    return _render_body(releases)


def _render_loading() -> str:
    return '<div class="welcome-empty">📜 正在从 GitHub Releases 拉取更新日志...</div>'


def _render_error(msg: str) -> str:
    return (
        f'<div class="welcome-empty">⚠️ 加载更新日志失败：{escape(msg)}'
        '<br><span style="opacity:0.7">检查网络后切换 mode 重试</span></div>'
    )


def _render_body(releases: list) -> str:
    """左列版本列表 + 右列描述（CSS-only 切换）

    为什么不用 <script>：主程序 viewer 经 updateContent() 以 innerHTML 注入本插件
    返回的 HTML 片段，HTML5 标准下 innerHTML 注入的 <script> 不会执行（2026-09-06
    版本点击无反应的根因）。改用 radio + :checked 兄弟选择器纯 CSS 切换：

    - radio 作为 .changelog-shell 的前置兄弟节点，供 ``#cl-rN:checked ~`` 联动
    - <label for=cl-rN> 点击切换 checked（浏览器原生行为，无需 JS）
    - 高亮 / 详情显示规则按版本数动态生成（≤20 条 × 2）
    """
    items = []
    bodies = []
    radios = []
    css_rules = []
    shown = releases[:20]
    for i, r in enumerate(shown):
        tag = escape(r.get("tag_name") or r.get("name") or f"v{i + 1}")
        date = escape((r.get("published_at") or "")[:10])
        body_html = r.get("body_html") or "<em>无更新说明</em>"
        checked = " checked" if i == 0 else ""
        radios.append(
            f'<input type="radio" name="cl-radio" id="cl-r{i}" '
            f'class="cl-radio"{checked}>'
        )
        items.append(
            f'<li class="changelog-version">'
            f'<label for="cl-r{i}">'
            f'<div class="ver-tag">{tag}</div>'
            f'<div class="ver-date">{date}</div></label></li>'
        )
        bodies.append(
            f'<div class="changelog-body cl-b{i}">{body_html}</div>'
        )
        css_rules.append(
            f'#cl-r{i}:checked ~ .changelog-versions label[for="cl-r{i}"] '
            f'{{ background: var(--accent-soft-strong); }}'
        )
        css_rules.append(
            f'#cl-r{i}:checked ~ .changelog-detail .cl-b{i} {{ display: block; }}'
        )

    return (
        f"<style>{_CHANGELOG_CSS}" + "\n" + "\n".join(css_rules) + "</style>"
        '<div class="changelog-shell">'
        f'{"".join(radios)}'
        f'<ul class="changelog-versions">{"".join(items)}</ul>'
        f'<div class="changelog-detail">{"".join(bodies)}</div>'
        "</div>"
    )


# ── 内嵌 CSS：复用主程序 viewer 的 :root CSS 变量（--accent-*）────────────
# 版本切换纯 CSS：.cl-bN 默认隐藏，由动态生成的 :checked 规则控制显示（见 _render_body）
_CHANGELOG_CSS = f"""
.changelog-shell {{
    display: flex;
    gap: 12px;
    margin-top: 6px;
    min-height: 200px;
}}
.changelog-versions {{
    list-style: none;
    padding: 0;
    margin: 0;
    min-width: 130px;
    max-width: 160px;
    border-right: 1px solid var(--accent-border-weak);
    overflow-y: auto;
    max-height: 360px;
}}
.changelog-version {{
    margin-bottom: 2px;
}}
.changelog-version label {{
    display: block;
    padding: 6px 10px;
    cursor: pointer;
    border-radius: 6px;
    transition: 0.15s ease;
}}
.changelog-version label:hover {{
    background: var(--accent-soft);
}}
.changelog-version .ver-tag {{
    font-weight: 600;
    color: var(--accent-text);
    font-size: {_TAG_FONT_PX}px;
}}
.changelog-version .ver-date {{
    font-size: {_TINY_FONT_PX}px;
    opacity: 0.6;
    margin-top: 2px;
}}
.changelog-detail {{
    flex: 1;
    min-width: 0;
    overflow-y: auto;
    max-height: 360px;
    padding-right: 4px;
}}
.changelog-body h1, .changelog-body h2, .changelog-body h3 {{
    color: var(--accent-text);
    margin-top: 0;
}}
.changelog-body img {{ max-width: 100%; }}
/* 默认全隐藏，由 _render_body 动态生成的 :checked 规则显示选中项 */
.changelog-body {{ display: none; }}
.cl-radio {{ display: none; }}
"""