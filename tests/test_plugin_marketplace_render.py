# -*- coding: utf-8 -*-
"""回归测试：插件市场刷新渲染不得出现「压缩帧」（行高塌缩堆左上角再展开）

复现背景：_render_plugins 全量重建时，QScrollArea 内容 widget 的高度更新
滞后于行创建（QVBoxLayout sizeHint 缓存惰性刷新），首帧行会被压缩成
几 px 堆在左上角，随后才展开——表现为刷新列表时内容先从左上角出现再展开。
修复：渲染期间隐藏新行，布局稳定后统一显示。
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
    return [
        {
            "name": f"plugin-{market}-{i:03d}",
            "version": "1.2.3",
            "description": "这是一个非常长的插件描述文本用于测试换行行为，" + "额外填充内容" * 6,
            "categories": ["工具"],
        }
        for i in range(n)
    ]


def _new_card(monkeypatch):
    """构造卡片：单市场源即时返回（模拟缓存命中），真实 show_card 流程"""
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
    return card


def _pump(seconds=0.2):
    app = _app()
    deadline = time.time() + seconds
    while time.time() < deadline:
        if app is not None:
            app.processEvents()
        time.sleep(0.01)


def test_render_rows_not_compressed(monkeypatch):
    """渲染稳定后：行高 == sizeHint 且内容高度 > 视口高度（无压缩帧）"""
    card = _new_card(monkeypatch)
    card.show()
    card.show_card()  # 真实流程：load_timer(50ms) → start_load → async_refresh + 300ms 首屏窗口

    # 等待行渲染完成并稳定展开（reveal 后 content 高度应超过视口）
    deadline = time.time() + 8
    rows = []
    while time.time() < deadline:
        _pump(0.05)
        rows = list(card._row_map.values())
        if rows and all(r.isVisible() for r in rows) and card._content.height() > card._scroll.viewport().height():
            break

    assert rows, "行未渲染"
    assert all(r.isVisible() for r in rows), "行未全部显示（reveal 失败）"

    vp_h = card._scroll.viewport().height()
    content_h = card._content.height()
    print(
        f"  rows={len(rows)} row0_h={rows[0].height()} sizeHint={rows[0].sizeHint().height()} "
        f"content_h={content_h} vp_h={vp_h} scrollMax={card._scroll.verticalScrollBar().maximum()}"
    )

    # 核心断言：行高必须与 sizeHint 一致（未压缩）；内容高度必须超过视口（列表真实展开）
    for r in rows[:5]:
        assert r.height() >= r.sizeHint().height() * 0.85, (
            f"行被压缩: actual={r.height()} sizeHint={r.sizeHint().height()}"
        )
    assert content_h > vp_h, f"内容未展开: content={content_h} viewport={vp_h}"
    assert card._scroll.verticalScrollBar().maximum() > 0, "滚动范围缺失（内容未撑开）"


def test_refresh_rebuild_not_compressed(monkeypatch):
    """刷新（全量重建）后同样不得出现压缩帧"""
    card = _new_card(monkeypatch)
    card.show()
    card.show_card()

    deadline = time.time() + 8
    while time.time() < deadline:
        _pump(0.05)
        if card._row_map:
            break
    assert card._row_map, "首屏未渲染"

    # 触发全量重建：第二个市场数据到达（模拟刷新时多市场逐批到达）
    card._merge_market_data({"name": "fake", "plugins": _make_plugins(40, "m2")})
    card._flush_render()

    deadline = time.time() + 5
    rows = []
    while time.time() < deadline:
        _pump(0.05)
        rows = list(card._row_map.values())
        if rows and all(r.isVisible() for r in rows) and card._content.height() > card._scroll.viewport().height():
            break

    assert rows and all(r.isVisible() for r in rows), "重建后行未显示"
    vp_h = card._scroll.viewport().height()
    content_h = card._content.height()
    print(
        f"  rebuild: rows={len(rows)} row0_h={rows[0].height()} sizeHint={rows[0].sizeHint().height()} "
        f"content_h={content_h} vp_h={vp_h}"
    )

    for r in rows[:5]:
        assert r.height() >= r.sizeHint().height() * 0.85, (
            f"重建后行被压缩: actual={r.height()} sizeHint={r.sizeHint().height()}"
        )
    assert content_h > vp_h, f"重建后内容未展开: content={content_h} viewport={vp_h}"


def test_load_more_no_blank_tail(monkeypatch):
    """「加载更多」后不得出现底部大段空白（滚动范围/内容高度残留）

    复现背景：行创建初期描述 QLabel 在未布局窄宽度下按 wordWrap 算出
    4 倍异常高度，冻结进 QWidget::sizeHint(totalSizeHint) 缓存；QScrollArea
    用该值撑开内容 + 清空后高度残留 → 底部可滚到大段空白。
    """
    card = _new_card(monkeypatch)
    card.show()
    card.show_card()

    deadline = time.time() + 8
    while time.time() < deadline:
        _pump(0.05)
        if card._row_map:
            break
    assert card._row_map, "首屏未渲染"

    # 点「加载更多」渲染剩余行
    card._on_load_more()
    deadline = time.time() + 5
    while time.time() < deadline:
        _pump(0.05)
        rows = list(card._row_map.values())
        if rows and all(r.isVisible() for r in rows):
            break
    assert rows and all(r.isVisible() for r in rows), "加载更多后行未显示"
    vp_h = card._scroll.viewport().height()
    content_h = card._content.height()
    size_h = card._content.sizeHint().height()
    scroll_max = card._scroll.verticalScrollBar().maximum()
    print(f"  load-more: rows={len(rows)} content_h={content_h} sizeHint_h={size_h} vp_h={vp_h} scrollMax={scroll_max}")

    # sizeHint 不得异常放大（修复前可达内容高度的 4 倍）
    assert size_h <= content_h * 1.5 + 50, f"sizeHint 异常放大: sizeHint={size_h} content={content_h}"
    # 滚动范围不得超出内容（修复前 scrollMax 远超实际内容 → 空白）
    assert scroll_max <= content_h - vp_h + 1, f"滚动范围超出内容: scrollMax={scroll_max} content-vp={content_h - vp_h}"

    # 竞态场景：加载更多后立即全量重建，不得残留旧高度/滚动范围
    card._render_plugins(_make_plugins(40, "m2"))
    vp_h2 = card._scroll.viewport().height()
    content_h2 = card._content.height()
    scroll_max2 = card._scroll.verticalScrollBar().maximum()
    size_h2 = card._content.sizeHint().height()
    print(f"  rebuild: content_h={content_h2} sizeHint_h={size_h2} vp_h={vp_h2} scrollMax={scroll_max2}")
    # 重建完成（行可见展开）后校验
    deadline = time.time() + 5
    while time.time() < deadline:
        _pump(0.05)
        rows2 = list(card._row_map.values())
        if rows2 and all(r.isVisible() for r in rows2) and card._content.height() > card._scroll.viewport().height():
            break
    content_h3 = card._content.height()
    size_h3 = card._content.sizeHint().height()
    scroll_max3 = card._scroll.verticalScrollBar().maximum()
    assert size_h3 <= content_h3 * 1.5 + 50, f"重建后 sizeHint 异常: sizeHint={size_h3} content={content_h3}"
    assert scroll_max3 <= content_h3 - vp_h2 + 1, f"重建后滚动范围超出内容: scrollMax={scroll_max3}"
