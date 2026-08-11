# -*- coding: utf-8 -*-
"""回归测试：插件市场搜索不得缺行（搜索结果必须包含全部匹配插件）

复现背景：_reconcile_rows（搜索过滤）复用已有行，原补行逻辑按
_rendered_count 连续索引续渲染 _matched[start:end]。但搜索重排后
_row_map 的行集合与 _matched 前部不再一致（旧行 ≠ 新匹配列表前 N 个），
导致匹配列表前部新出现的插件永远缺行 → 搜索「内容不全」。

复现场景：首屏渲染 p00..p29（30 行）。搜索 "3" 命中 13 个：
p03/p13/p23（首屏内，已有行）+ p30..p39（首屏外，缺行）。
修复前只显示 3 个已渲染的，p30..p39 永远缺行。

修复：_render_next_batch 改为按 _matched 顺序补齐「尚无 widget 的插件」，
_reconcile_rows/_refresh_row_states 无条件调用它。
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MARKETPLACE = ROOT / "plugins" / "plugin-marketplace"
if str(PLUGIN_MARKETPLACE) not in sys.path:
    sys.path.insert(0, str(PLUGIN_MARKETPLACE))


def _app():
    from PyQt5.QtWidgets import QApplication

    return QApplication.instance()


def _make_plugins(n, market="m"):
    """名字 p00..p(n-1)：搜索词可精确控制命中集合"""
    return [
        {
            "name": f"p{i:02d}",
            "version": "1.2.3",
            "description": "测试插件描述文本",
            "categories": ["工具"],
        }
        for i in range(n)
    ]


def _new_card(monkeypatch):
    if str(PLUGIN_MARKETPLACE) not in sys.path:
        sys.path.insert(0, str(PLUGIN_MARKETPLACE))
    from ui.cards import MarketplaceCard
    from ui.marketplace_manager import MarketplaceSourceManager

    monkeypatch.setattr(
        MarketplaceSourceManager,
        "get_sources",
        lambda self: [{"name": "fake", "source": {"source": "url", "url": "x"}}],
    )
    monkeypatch.setattr(
        MarketplaceSourceManager,
        "fetch_marketplace",
        lambda self, src, force=False: {"name": "fake", "plugins": _make_plugins(40)},
    )
    card = MarketplaceCard()
    # 隔离本地已安装插件（真实环境 .drifox 有 minimax-h3 等，名字/描述含数字会干扰搜索断言）
    card._build_local_extra_plugins = lambda: []
    return card


def _pump(seconds=0.2):
    app = _app()
    deadline = time.time() + seconds
    while time.time() < deadline:
        if app is not None:
            app.processEvents()
        time.sleep(0.01)


def _wait_rows(card, n, timeout=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _pump(0.05)
        if len(card._row_map) >= n:
            return True
    return False


def test_search_renders_matches_outside_first_batch(monkeypatch):
    """搜索命中「首屏 30 行之外」的插件时，必须补齐缺失行（不丢结果）

    40 个插件 p00..p39，首屏渲染 30 行（p00..p29）。搜索 "3" 命中 13 个：
    p03/p13/p23（首屏内）+ p30..p39（首屏外）。修复前只显示首屏内的 3 个。
    """
    card = _new_card(monkeypatch)
    card.show()
    card.show_card()
    assert _wait_rows(card, 30), f"首屏未渲染满 30 行: {len(card._row_map)}"

    card._search_edit.setText("3")
    card._filter_plugins()
    _pump(0.5)

    visible = sorted(r._meta["name"] for r in card._row_map.values() if r.isVisible())
    expected = sorted({"p03", "p13", "p23"} | {f"p{i:02d}" for i in range(30, 40)})
    print(f"  搜索 '3': visible={len(visible)} expected={len(expected)} -> {visible}")
    assert len(visible) == len(expected), f"搜索结果缺行: 显示 {len(visible)} 个, 期望 {len(expected)} 个"
    assert visible == expected, f"搜索结果集合不符: {visible} != {expected}"
    # 状态栏计数一致
    assert card._matched and len(card._matched) == len(expected)

    # 清空搜索：恢复全部 40 个匹配，全部可见（行已补齐）
    card._search_edit.setText("")
    card._filter_plugins()
    _pump(0.5)
    visible2 = sorted(r._meta["name"] for r in card._row_map.values() if r.isVisible())
    assert len(visible2) == 40, f"清空搜索后应显示 40 行, 实际 {len(visible2)}"


def test_search_match_less_than_rendered_still_complete(monkeypatch):
    """搜索命中数 < 已渲染行数但含未渲染插件：同样必须补齐

    搜索 "2" 命中名字含 '2' 的插件：p02/p12/p32（首屏内）+ p20..p29（首屏外）。
    即使 len(_matched) < len(_row_map)，缺行也必须补。
    """
    card = _new_card(monkeypatch)
    card.show()
    card.show_card()
    assert _wait_rows(card, 30), f"首屏未渲染满 30 行: {len(card._row_map)}"

    card._search_edit.setText("2")
    card._filter_plugins()
    _pump(0.5)

    visible = sorted(r._meta["name"] for r in card._row_map.values() if r.isVisible())
    expected = sorted({"p02", "p12", "p32"} | {f"p{i:02d}" for i in range(20, 30)})
    print(f"  搜索 '2': visible={len(visible)} expected={len(expected)}")
    assert visible == expected, f"搜索结果集合不符: {visible} != {expected}"


def test_search_no_match_shows_empty_state(monkeypatch):
    """搜索无命中：空态提示 + 无「加载更多」按钮 + 状态栏计数 0"""
    card = _new_card(monkeypatch)
    card.show()
    card.show_card()
    assert _wait_rows(card, 30)

    card._search_edit.setText("zzz-no-match")
    card._filter_plugins()
    _pump(0.5)

    assert card._matched == [], "无命中时 _matched 应为空"
    assert card._load_more_btn is None, "无命中时不应有「加载更多」按钮"
    assert card._content_stack.currentIndex() == 1, "无命中时应显示空态页"
    assert "没有匹配" in card._empty_label.text(), f"空态文案不符: {card._empty_label.text()}"
