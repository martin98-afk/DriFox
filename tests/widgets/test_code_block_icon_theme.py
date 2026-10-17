# -*- coding: utf-8 -*-
"""代码块工具栏图标必须跟随主题（浅色模式下发白回归）。

症状（2026-10-11）：浅色模式下消息卡片代码块右上角的「保存/复制」两个按钮是**白色**的
（深色图标集的原色），在浅色卡片上几乎看不见。

根因：`card_render_core._ICON_PREFIX_CACHE` 是**模块级可变全局**，由 `_update_icon_prefix()`
用 `global` 重新绑定（深色 `qrc:/icons` ↔ 浅色 `qrc:/icons_light`）。而 `card_viewers`
顶部按值 `from ... import _ICON_PREFIX_CACHE`，拿到的是导入那一刻的旧值、**永远不再更新**；
线程池渲染快照（`_collect_render_snapshot`）正是用它当 `icon_prefix` → 异步渲染出来的
代码块永远带白色图标（主线程路径走的是函数默认值，实时读全局，所以正常）。

修法：新增 `current_icon_prefix()` 实时读法，快照侧改用它并在取快照前补一次
`_update_icon_prefix()`（覆盖「浅色冷启动首个渲染就走异步」的场景）。

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

    # 绕开 LRU（key 里没有图标前缀，跨用例复用时会把旧主题产物带进来）
    return _crc._render_markdown_to_html_cached_impl.__wrapped__(md)


def test_wrapper_follows_light_prefix(crc, monkeypatch):
    monkeypatch.setattr(crc, "get_tool_qrc_prefix", lambda: "qrc:/icons_light")
    crc._update_icon_prefix()
    html = _render_code_block()
    assert "qrc:/icons_light/复制.svg" in html, "浅色主题必须用浅色图标集"
    assert "qrc:/icons_light/导入.svg" in html
    assert "qrc:/icons/复制.svg" not in html, "不得残留深色（白色）图标"


def test_wrapper_follows_dark_prefix(crc, monkeypatch):
    monkeypatch.setattr(crc, "get_tool_qrc_prefix", lambda: "qrc:/icons")
    crc._update_icon_prefix()
    html = _render_code_block()
    assert "qrc:/icons/复制.svg" in html
    assert "qrc:/icons_light/复制.svg" not in html


def test_prefix_switches_back_and_forth(crc, monkeypatch):
    """深→浅→深：每次都必须取到当前主题的前缀（回归「导入期快照永不更新」）。"""
    for prefix in ("qrc:/icons", "qrc:/icons_light", "qrc:/icons"):
        monkeypatch.setattr(crc, "get_tool_qrc_prefix", lambda p=prefix: p)
        crc._update_icon_prefix()
        assert crc.current_icon_prefix() == prefix
        assert f"{prefix}/复制.svg" in _render_code_block()


def test_render_snapshot_uses_live_prefix(crc, monkeypatch):
    """线程池快照必须带实时前缀（本次回归的直接病灶）。"""
    from app.widgets.card_viewers import CodeWebViewer

    monkeypatch.setattr(crc, "get_tool_qrc_prefix", lambda: "qrc:/icons_light")
    stub = types.SimpleNamespace(_streaming=False, _thinking_finalized=False, _is_history=False)
    snap = CodeWebViewer._collect_render_snapshot(stub, "hello", False)
    assert snap["icon_prefix"] == "qrc:/icons_light"


def test_render_snapshot_refreshes_stale_cache(crc, monkeypatch):
    """浅色冷启动：缓存还是深色默认值时，取快照这一拍必须自我纠正。"""
    from app.widgets.card_viewers import CodeWebViewer

    monkeypatch.setattr(crc, "_ICON_PREFIX_CACHE", "qrc:/icons")  # 模拟未刷新的冷启动
    monkeypatch.setattr(crc, "get_tool_qrc_prefix", lambda: "qrc:/icons_light")
    stub = types.SimpleNamespace(_streaming=False, _thinking_finalized=False, _is_history=False)
    snap = CodeWebViewer._collect_render_snapshot(stub, "hello", False)
    assert snap["icon_prefix"] == "qrc:/icons_light"


def test_no_value_import_of_icon_prefix():
    """静态守卫：禁止再按值导入 `_ICON_PREFIX_CACHE`（会永久陈旧）。"""
    from app.widgets import card_viewers

    assert not hasattr(card_viewers, "_ICON_PREFIX_CACHE"), (
        "card_viewers 不得持有 _ICON_PREFIX_CACHE 的按值导入；请用 current_icon_prefix()"
    )
