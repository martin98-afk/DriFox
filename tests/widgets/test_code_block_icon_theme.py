# -*- coding: utf-8 -*-
"""代码块主题解耦契约：高亮走 class，图标走 CSS mask。

背景（2026-10-30 主题切换重构）：此前 Pygments `noclasses=True` 把高亮色内联进
每个 token span 的 `style`，代码块图标按 `qrc:/icons` vs `qrc:/icons_light` 前缀
写死进 `<img src>`。两个因素叠加的后果：HTML 与主题绑定，主题一变整份 HTML 过期，
只能逐卡重跑 markdown + Pygments（单卡 40~120ms）。

改造后的契约（本文件锁死）：
1. 代码块 HTML **不含** 任何 qrc 前缀与内联高亮色，图标是 `.ci .ci-save/.ci-copy`；
2. 高亮色全部来自 `card_render_core.get_pygments_css(style)`，只生成 token 级规则
   （裸 `pre {}` / `.code-content pre {}` 会污染容器底色，必须被过滤）；
3. 深浅两套风格产出不同 CSS，且按 style 名缓存。

本文件不加载页面、不需要 WebEngine 上下文。
"""

import types

import pytest


@pytest.fixture()
def crc(monkeypatch):
    """导入 card_render_core，并在用例结束后还原图标前缀缓存。"""
    from app.widgets import card_render_core as _crc

    monkeypatch.setattr(_crc, "_ICON_PREFIX_CACHE", _crc._ICON_PREFIX_CACHE)
    return _crc


def _render_code_block(md: str = "```python\nprint(1)\n```\n") -> str:
    from app.widgets import card_render_core as _crc

    # 绕开 LRU（跨用例复用会带入上一轮的产物）
    return _crc._render_markdown_to_html_cached_impl.__wrapped__(md)


def test_code_block_html_is_theme_agnostic(crc):
    """代码块 HTML 不得携带主题信息（前缀/内联高亮色）。"""
    html = _render_code_block()
    assert "qrc:/icons" not in html, "图标前缀不得写进 HTML（主题切换后必须零改动）"
    assert "qrc:/icons_light" not in html
    assert "color: #" not in html.replace("color: var(", ""), "高亮色不得内联进 span"
    assert "ci-save" in html and "ci-copy" in html, "图标必须走 CSS mask 类"


def test_highlight_tokens_use_class(crc):
    """高亮 token 必须带 class（noclasses=False），否则切主题无法靠 CSS 换色。"""
    html = _render_code_block("```python\ndef f():\n    return 1\n```\n")
    assert 'class="k"' in html or 'class="nf"' in html
    assert 'style="color' not in html.split("<pre>")[1].split("</pre>")[0]


def test_pygments_css_only_token_rules(crc):
    """生成的高亮 CSS 只能有 token 规则：裸规则会覆盖代码块/diff 容器底色。"""
    css = crc.get_pygments_css("dracula")
    lines = [ln.strip() for ln in css.splitlines() if ln.strip()]
    assert lines, "高亮 CSS 不得为空"
    assert not any(ln.startswith("pre {") for ln in lines), "裸 pre 规则会污染全局 pre"
    assert not any(ln.startswith(".code-content pre {") for ln in lines), "容器底色规则必须过滤"
    assert all(ln.startswith((".code-content pre .", ".line-code .")) for ln in lines)
    # diff 与代码块两个作用域都要覆盖
    assert any(ln.startswith(".line-code .") for ln in lines)


def test_pygments_css_tracks_style(crc):
    """深/浅两套风格必须产出不同 CSS（切主题才有效果）。"""
    dark = crc.get_pygments_css("dracula")
    light = crc.get_pygments_css("friendly")
    assert dark != light
    assert dark is crc.get_pygments_css("dracula"), "同一风格必须命中缓存"


def test_render_snapshot_uses_live_prefix(crc, monkeypatch):
    """渲染快照仍携带 icon_prefix 键（线程池 API 兼容，虽已不写进 HTML）。"""
    from app.widgets.card_viewers import CodeWebViewer

    monkeypatch.setattr(crc, "get_tool_qrc_prefix", lambda: "qrc:/icons_light")
    stub = types.SimpleNamespace(
        _streaming=False,
        _thinking_finalized=False,
        _is_history=False,
        _tool_lazy_body=None,
    )
    snap = CodeWebViewer._collect_render_snapshot(stub, "hello", False)
    assert snap["icon_prefix"] == "qrc:/icons_light"


def test_render_snapshot_refreshes_stale_cache(crc, monkeypatch):
    """浅色冷启动：缓存还是深色默认值时，取快照这一拍必须自我纠正。"""
    from app.widgets.card_viewers import CodeWebViewer

    monkeypatch.setattr(crc, "_ICON_PREFIX_CACHE", "qrc:/icons")
    monkeypatch.setattr(crc, "get_tool_qrc_prefix", lambda: "qrc:/icons_light")
    stub = types.SimpleNamespace(
        _streaming=False,
        _thinking_finalized=False,
        _is_history=False,
        _tool_lazy_body=None,
    )
    snap = CodeWebViewer._collect_render_snapshot(stub, "hello", False)
    assert snap["icon_prefix"] == "qrc:/icons_light"


def test_no_value_import_of_icon_prefix():
    """静态守卫：禁止再按值导入 `_ICON_PREFIX_CACHE`（会永久陈旧）。"""
    from app.widgets import card_viewers

    assert not hasattr(card_viewers, "_ICON_PREFIX_CACHE"), (
        "card_viewers 不得持有 _ICON_PREFIX_CACHE 的按值导入；请用 current_icon_prefix()"
    )
