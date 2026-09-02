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
    """左列版本列表 + 右列描述（SPA，JS 切换不调 Python）"""
    items = []
    bodies = []
    for i, r in enumerate(releases[:20]):
        tag = escape(r.get("tag_name") or r.get("name") or f"v{i + 1}")
        date = escape((r.get("published_at") or "")[:10])
        body_html = r.get("body_html") or "<em>无更新说明</em>"
        active = "active" if i == 0 else ""
        items.append(
            f'<li class="changelog-version {active}" data-idx="{i}">'
            f'<div class="ver-tag">{tag}</div>'
            f'<div class="ver-date">{date}</div></li>'
        )
        bodies.append(
            f'<div class="changelog-body" data-idx="{i}" '
            f'style="{"display:block" if i == 0 else "display:none"}">{body_html}</div>'
        )

    return (
        f"<style>{_CHANGELOG_CSS}</style>"
        '<div class="changelog-shell">'
        f'<ul class="changelog-versions">{"".join(items)}</ul>'
        f'<div class="changelog-detail">{"".join(bodies)}</div>'
        "</div>"
        f"<script>{_CHANGELOG_JS}</script>"
    )


# ── 内嵌 CSS：复用主程序 viewer 的 :root CSS 变量（--accent-*）────────────
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
    padding: 6px 10px;
    cursor: pointer;
    border-radius: 6px;
    margin-bottom: 2px;
    transition: 0.15s ease;
}}
.changelog-version:hover {{
    background: var(--accent-soft);
}}
.changelog-version.active {{
    background: var(--accent-soft-strong);
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
"""


# ── 内嵌 JS：版本点击 → SPA 切换 body（DOM 内部操作，不调 Python）──────────
_CHANGELOG_JS = """
(function(){
    document.addEventListener('click', function(e){
        var verItem = e.target && e.target.closest && e.target.closest('.changelog-version');
        if (!verItem) return;
        e.stopPropagation();
        e.preventDefault();
        var vIdx = verItem.getAttribute('data-idx');
        var vShell = verItem.closest('.changelog-shell');
        if (!vShell) return;
        vShell.querySelectorAll('.changelog-version').forEach(function(el){ el.classList.remove('active'); });
        vShell.querySelectorAll('.changelog-body').forEach(function(el){ el.style.display = 'none'; });
        verItem.classList.add('active');
        var vBody = vShell.querySelector('.changelog-body[data-idx="' + vIdx + '"]');
        if (vBody) vBody.style.display = 'block';
    });
})();
"""