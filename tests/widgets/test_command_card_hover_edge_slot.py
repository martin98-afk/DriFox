# -*- coding: utf-8 -*-
"""CommandCard 视口底「半行 item」hover 触发异常滚动 回归测试

== 问题描述 ==
命令卡片含分区分隔线时，卡片高度按「8 项 × 行高 + 全部分隔线数」预算。
位于末个可见项之后的分隔线也计入预算，把视口撑高 1~N px，
使下一个 item 在视口底露出 1~N px 边条。鼠标扫过边条即 hover 选中
（hover 即选中）→ _scroll_to_item 强制完整可见 → 列表凭空下移一格。

用户感知：「卡片高度稍微超过 8 个 item 一点点，鼠标放到最下面扫过去
列表会往下跳一格」。

== 根因 ==
_apply_list_height 高度口径 = visible * ITEM_HEIGHT + divider_count(全列表)。
视口外分隔线不应计入视口预算。正确口径：第 N 个 item 槽的结束 y
（其间分隔线自然包含，其后分隔线自然排除）。

== 修复 ==
_apply_list_height 改用 _natural_height_for(visible)：
按 _virtual_slots 取第 visible 个 item 槽的结束 y 作为视口高度。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget

_REF_HOLDER = []  # 防 GC：测试结束后 parent/card 被 GC 析构会触发 Qt 析构竞态崩溃


def _ensure_qapp():
    return QApplication.instance() or QApplication([])


def _make_card_with_items(items):
    """带父窗口的 CommandCard（800x600 预算充足），注入 items 并全量渲染"""
    from app.widgets.cards.card_container import BottomCardContainer
    from app.widgets.cards.floating.command_card import CommandCard

    parent = QWidget()
    parent.setLayout(QVBoxLayout())
    parent.resize(800, 600)
    container = BottomCardContainer()
    parent.layout().addWidget(container)
    card = CommandCard()
    container.add_card("test", card)
    card._all_items_cache = list(items)
    card._cache_dirty = False
    card._filtered_items = list(items)
    card._render(incremental=False)
    parent.show()
    card.setVisible(True)
    app = QApplication.instance()
    for _ in range(10):
        app.processEvents()
    _REF_HOLDER.append(parent)
    return parent, card


# 16 项 4 区：divider 分别位于 idx7（cmd|ui）、idx8（ui|skill）、idx13（skill|agent）前
# → 前 8 个 item 之间含 2 条 divider，第 3 条在视口外
_ITEMS_16_FOUR_SECTIONS = (
    [{"name": f"cmd{i}", "type": "command", "subtype": "", "description": ""} for i in range(7)]
    + [{"name": "ui0", "type": "command", "subtype": "ui_plugin", "description": ""}]
    + [{"name": f"skill{i}", "type": "skill", "description": ""} for i in range(5)]
    + [{"name": f"agent{i}", "type": "agent", "description": ""} for i in range(3)]
)

# 9 项 2 区：唯一 divider 在 idx8 前（视口外）
_ITEMS_9_TWO_SECTIONS = [{"name": f"cmd{i}", "type": "command", "subtype": "", "description": ""} for i in range(8)] + [
    {"name": "skill0", "type": "skill", "description": ""}
]


def _visible_item_slots(card):
    """[(idx, y)] 视口内绑定且顶边可见的 item 槽"""
    from app.widgets.cards.floating.command_card import ITEM_HEIGHT

    view_h = card._scroll_area.viewport().height()
    out = []
    for slot in sorted(card._slot_widgets.keys()):
        kind, idx, y = card._virtual_slots[slot]
        w = card._slot_widgets[slot]
        if kind != "item" or idx is None:
            continue
        if not w.isVisible() or y >= view_h:
            continue
        out.append((idx, y, ITEM_HEIGHT))
    return out


def _nth_item_end_y(card, n):
    """第 n 个（1 基）item 槽的结束 y"""
    count = 0
    for kind, _idx, y in card._virtual_slots:
        if kind != "item":
            continue
        count += 1
        if count == n:
            return y + 36
    return None


def test_no_partial_item_row_at_viewport_bottom():
    """视口底不允许出现「半行 item」：任何顶边可见的 item 必须完整可见"""
    _ensure_qapp()
    parent, card = _make_card_with_items(_ITEMS_16_FOUR_SECTIONS)
    try:
        assert card._divider_count == 3, "期望 3 条分区分隔线"
        view_h = card._scroll_area.viewport().height()
        for idx, y, h in _visible_item_slots(card):
            assert y + h <= view_h, (
                f"item{idx} 顶边 y={y} 高 {h} 超出视口 {view_h}px，"
                f"露出 {y + h - view_h}px 半行 → 鼠标扫过会触发 hover 滚动"
            )
    finally:
        parent.hide()
        _REF_HOLDER.append(parent)


def test_card_height_equals_nth_item_end():
    """卡片高度 = 第 8 个 item 槽的结束 y（自然视口口径，不含视口外分隔线）"""
    _ensure_qapp()
    parent, card = _make_card_with_items(_ITEMS_16_FOUR_SECTIONS)
    try:
        expected = _nth_item_end_y(card, 8)
        assert expected is not None
        assert card.height() == expected, (
            f"卡片高度 {card.height()} 应 = 第 8 个 item 槽结束 y {expected}"
            f"（旧口径 8*36+3=291 会把视口外分隔线计入，撑出半行 item）"
        )
    finally:
        parent.hide()
        parent.deleteLater()


def test_hover_last_visible_item_does_not_scroll():
    """hover 视口内最后一项不应触发滚动（旧口径下会滚 35px）"""
    _ensure_qapp()
    from app.widgets.cards.floating.command_card import CommandItemWidget

    parent, card = _make_card_with_items(_ITEMS_16_FOUR_SECTIONS)
    try:
        sb = card._scroll_area.verticalScrollBar()
        view_h = card._scroll_area.viewport().height()
        slots = _visible_item_slots(card)
        assert slots, "应有可见 item 槽"
        last_idx = max(idx for idx, _y, _h in slots)
        # 找到最后一项的 widget 并 hover
        for slot in sorted(card._slot_widgets.keys()):
            kind, idx, y = card._virtual_slots[slot]
            if kind == "item" and idx == last_idx:
                w = card._slot_widgets[slot]
                assert isinstance(w, CommandItemWidget)
                card._on_item_hovered(w)
                for _ in range(3):
                    QApplication.instance().processEvents()
                break
        assert sb.value() == 0, (
            f"hover 视口内最后一项（item{last_idx}，y+36<= {view_h}）后滚动条 value={sb.value()}，应为 0（不应滚动）"
        )
    finally:
        parent.hide()
        parent.deleteLater()


def test_divider_outside_viewport_no_edge_slot_9items():
    """9 项 2 区：divider 在第 9 项前（视口外）时，第 9 项不得在视口内露头"""
    _ensure_qapp()
    parent, card = _make_card_with_items(_ITEMS_9_TWO_SECTIONS)
    try:
        assert card._divider_count == 1
        view_h = card._scroll_area.viewport().height()
        # 第 9 项（idx8）的槽 y 必须 >= 视口高（完全不露头）
        for kind, idx, y in card._virtual_slots:
            if kind == "item" and idx == 8:
                assert y >= view_h, f"item8 槽 y={y} < 视口 {view_h}px，露出 {view_h - y}px 边条"
        assert card.height() == 8 * 36, f"卡片高度应 = 8*36=288，实际 {card.height()}"
    finally:
        parent.hide()
        parent.deleteLater()
