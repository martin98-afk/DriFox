# -*- coding: utf-8 -*-
"""T16 回归：渲染 LRU 缓存按主题版本分桶

背景
----
`_render_markdown_to_html_cached_impl` 的 lru key 含 theme_ver 尾参
（ThemeRefreshCoordinator.get_version()，由包装函数 `_render_markdown_to_html_cached`
显式传入）。同一 markdown 在深/浅主题下各存一份渲染结果：
- 深浅来回切换直接命中各自版本桶（此前每次切换都要 clear + 全量重渲）；
- 主题真变后旧版本条目永不命中，无需清缓存。

本文件锁定：
1. 同版本重复渲染命中缓存（不重算）；
2. 版本推进生成新条目，旧版本条目在 LRU 容量内仍保留、切回即命中；
3. >200KB 大文本 __wrapped__ 绕过分支签名兼容（theme_ver 默认值）。
"""

from app.utils.theme_refresh import ThemeRefreshCoordinator
from app.widgets.card_render_core import (
    _LRU_CACHE_SIZE_THRESHOLD,
    _render_markdown_to_html_cached,
    _render_markdown_to_html_cached_impl,
)


def _fake_version(monkeypatch, getter):
    """把 ThemeRefreshCoordinator.get_version 打成 getter() 的动态假值"""
    monkeypatch.setattr(ThemeRefreshCoordinator, "get_version", classmethod(lambda cls: getter()))


def test_same_theme_version_hits_cache(monkeypatch):
    """同版本重复渲染：misses 不增、hits 递增（不重算）"""
    _render_markdown_to_html_cached_impl.cache_clear()
    _fake_version(monkeypatch, lambda: 7)

    md = "# cache hit test\n\n正文内容"
    _render_markdown_to_html_cached(md)
    misses_first = _render_markdown_to_html_cached_impl.cache_info().misses

    _render_markdown_to_html_cached(md)
    _render_markdown_to_html_cached(md)
    info = _render_markdown_to_html_cached_impl.cache_info()
    assert info.misses == misses_first, "同版本重复渲染不应产生新 miss"
    assert info.hits >= 2, f"同版本重复渲染应命中缓存，hits={info.hits}"


def test_theme_version_bump_generates_new_entry_keeps_old(monkeypatch):
    """版本推进生成新条目；旧版本条目在 LRU 容量内仍保留，切回即命中"""
    _render_markdown_to_html_cached_impl.cache_clear()
    ver = {"v": 100}
    _fake_version(monkeypatch, lambda: ver["v"])

    md = "# versioned cache\n\n正文"

    _render_markdown_to_html_cached(md)  # v100：miss（新桶）
    _render_markdown_to_html_cached(md)  # v100：hit
    info_v100 = _render_markdown_to_html_cached_impl.cache_info()

    ver["v"] = 101  # 切主题 → 版本推进
    _render_markdown_to_html_cached(md)
    info_v101 = _render_markdown_to_html_cached_impl.cache_info()
    assert info_v101.misses == info_v100.misses + 1, "版本推进应生成新版本条目（恰好 +1 miss）"

    ver["v"] = 100  # 切回旧主题
    _render_markdown_to_html_cached(md)
    info_back = _render_markdown_to_html_cached_impl.cache_info()
    assert info_back.misses == info_v101.misses, "旧版本条目应仍保留（切回不再 miss）"
    assert info_back.hits == info_v101.hits + 1, "切回旧主题应直接命中旧条目"


def test_large_text_bypass_keeps_signature_compat(monkeypatch):
    """>200KB 大文本走 __wrapped__ 绕过缓存，theme_ver 默认值保证签名兼容"""
    _render_markdown_to_html_cached_impl.cache_clear()
    _fake_version(monkeypatch, lambda: 42)

    md = "x" * (_LRU_CACHE_SIZE_THRESHOLD + 1)
    html = _render_markdown_to_html_cached(md)  # 不抛 TypeError 即兼容
    assert html  # 非空渲染结果

    info = _render_markdown_to_html_cached_impl.cache_info()
    assert info.misses == 0, "大文本应绕过 LRU（不产生缓存条目）"
