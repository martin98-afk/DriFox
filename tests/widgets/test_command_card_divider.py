# -*- coding: utf-8 -*-
"""CommandCard 分隔线（divider）与高度计算一致性测试

问题背景：_render 的 incremental=True 路径不清理/不重建 dividers，
导致 _dividers 列表与 scroll_layout 实际状态脱节，_apply_list_height
计算高度时使用 stale divider_count，造成列表高度异常。

修复：_render 全量路径中移除 incremental 守卫，始终清理/重算 dividers。
"""

import os
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication


def _ensure_qapp():
    """确保 QApplication 可用"""
    return QApplication.instance() or QApplication(sys.argv)


# 测试用 items 构造（31 items, 4 sections → 3 dividers）
_ITEMS = []
for i in range(15):
    _ITEMS.append({"name": f"cmd{i}", "type": "command", "subtype": "", "description": f"cmd{i} desc"})
for i in range(3):
    _ITEMS.append({"name": f"ui{i}", "type": "command", "subtype": "ui_plugin", "description": f"ui{i} desc"})
for i in range(10):
    _ITEMS.append({"name": f"skill{i}", "type": "skill", "description": f"skill{i} desc"})
for i in range(3):
    _ITEMS.append({"name": f"agent{i}", "type": "agent", "description": f"agent{i} desc"})

_SUBSET = _ITEMS[:17]  # 15 cmd + 2 ui → dividers: 1

# divider 专用测试 items（description 为空，不触发 tooltip）
_ITEMS_NO_DESC = [{**it, "description": ""} for it in _ITEMS]
_SUBSET_NO_DESC = _ITEMS_NO_DESC[:17]


def _make_card(items=None):
    """创建 CommandCard 并用 _ITEMS 初始化缓存"""
    from app.widgets.cards.floating.command_card import CommandCard

    card = CommandCard()
    src = items if items is not None else _ITEMS
    card._all_items_cache = list(src)
    card._cache_dirty = False
    return card


_REF_HOLDER = []  # 防止局部 parent 被 GC 回收


def _card_with_tooltip():
    """创建 CommandCard，让 tooltip 可见（选中第一个有描述的 item）"""
    from PyQt5.QtWidgets import QWidget, QVBoxLayout
    from app.widgets.cards.floating.command_card import CommandCard
    from app.widgets.cards.card_container import BottomCardContainer

    parent = QWidget()
    parent.setLayout(QVBoxLayout())
    parent.resize(800, 600)
    container = BottomCardContainer()
    parent.layout().addWidget(container)
    card = CommandCard()
    container.add_card("test", card)
    # items 统一带 description
    items = [
        dict(name=f"cmd{i}", type="command", subtype="", description=f"cmd{i} description text") for i in range(25)
    ]
    card._all_items_cache = list(items)
    card._cache_dirty = False
    card._filtered_items = list(items)
    card._render(incremental=False)
    parent.show()
    card.setVisible(True)
    # 确保布局生效
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance()
    for _ in range(10):
        app.processEvents()
    # 让 tooltip 可见
    card._selected_index = 0
    card._last_selected_index = -1
    card._update_selection()
    for _ in range(5):
        app.processEvents()
    _REF_HOLDER.append(parent)
    return card


class TestCommandCardDivider:
    def test_floating_tooltip_does_not_affect_card_height(self):
        """悬浮描述气泡为独立顶层窗口，不应计入命令卡片自身高度

        重构后顶部描述以「悬浮气泡」形式浮在卡片上方（覆盖聊天区），
        不再位于卡片布局内，因此卡片高度只等于命令列表高度（含分区分隔线），
        描述多长都不会挤占列表剩余空间。
        """
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import (
            MAX_VISIBLE_ITEMS,
            ITEM_HEIGHT,
        )

        card = _card_with_tooltip()
        # 悬浮气泡作为独立窗口可见，且自身高度 > 0
        assert card._desc_tooltip_label.isVisible(), "悬浮气泡应可见"
        assert card._desc_tooltip_label.height() > 0, "悬浮气泡应有高度"

        # 卡片自身高度只等于列表高度（含分隔线），不含气泡
        total = len(card._item_widgets) + len(card._dividers)
        list_h = min(total, MAX_VISIBLE_ITEMS) * ITEM_HEIGHT + len(card._dividers)
        assert card.height() == list_h, f"card h {card.height()} 应 == 列表高度 {list_h}（气泡为独立悬浮窗口，不计入）"

    def test_incremental_false_full_render(self):
        """incremental=False 全量渲染应有正确数量的 dividers"""
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import (
            MAX_VISIBLE_ITEMS,
            ITEM_HEIGHT,
        )

        card = _make_card(items=_ITEMS_NO_DESC)
        card._filtered_items = list(_ITEMS_NO_DESC)
        card._render(incremental=False)

        assert len(card._dividers) == 3, f"期望 3 个 dividers, 实际 {len(card._dividers)}"
        total = len(card._item_widgets) + len(card._dividers)
        visible = min(total, MAX_VISIBLE_ITEMS)
        expected_h = visible * ITEM_HEIGHT + len(card._dividers)
        assert card.height() == expected_h, f"高度不匹配: {card.height()} != {expected_h}"
        assert card._scroll_layout.count() == total, "scroll_layout 元素数与 widgets+dividers 不一致"

    def test_incremental_true_fresh(self):
        """incremental=True 首次渲染（不经 False）应有正确 dividers"""
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import (
            MAX_VISIBLE_ITEMS,
            ITEM_HEIGHT,
        )

        card = _make_card(items=_ITEMS_NO_DESC)
        card._filtered_items = list(_ITEMS_NO_DESC)
        card._render(incremental=True)

        assert len(card._dividers) == 3, f"incremental=True 首次: 期望 3 dividers, 实际 {len(card._dividers)}"
        total = len(card._item_widgets) + len(card._dividers)
        visible = min(total, MAX_VISIBLE_ITEMS)
        expected_h = visible * ITEM_HEIGHT + len(card._dividers)
        assert card.height() == expected_h, f"高度不匹配: {card.height()} != {expected_h}"

    def test_incremental_true_same_items(self):
        """incremental=True + items 未变 → 走快速路径，dividers 不变"""
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import (
            MAX_VISIBLE_ITEMS,
            ITEM_HEIGHT,
        )

        # 放到一个有合理高度的窗口中，避免 _available_card_budget() 因
        # self.window() == self（无父窗口）而错误压缩高度
        from PyQt5.QtWidgets import QWidget, QVBoxLayout

        _parent = QWidget()
        _parent.resize(800, 600)
        _parent.setLayout(QVBoxLayout())
        card = _make_card(items=_ITEMS_NO_DESC)
        _parent.layout().addWidget(card)
        _REF_HOLDER.append(_parent)
        card._filtered_items = list(_ITEMS_NO_DESC)
        card._render(incremental=False)
        pre_div = len(card._dividers)
        pre_h = card.height()

        card._render(incremental=True)
        assert len(card._dividers) == pre_div, f"快速路径 dividers 不应变: {pre_div} -> {len(card._dividers)}"
        # 快速路径会调用 _apply_list_height() 重新计算高度，
        # 在有父窗口时预算充足，高度应保持不变
        assert card.height() == pre_h, f"快速路径高度不应变: {pre_h} -> {card.height()}"

    def test_filter_then_back_to_full(self):
        """过滤子集 → 恢复全集，dividers 数量先降后升"""
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import (
            MAX_VISIBLE_ITEMS,
            ITEM_HEIGHT,
        )
        from PyQt5.QtWidgets import QWidget, QVBoxLayout

        _parent = QWidget()
        _parent.resize(800, 600)
        _parent.setLayout(QVBoxLayout())
        card = _make_card(items=_ITEMS_NO_DESC)
        _parent.layout().addWidget(card)
        _REF_HOLDER.append(_parent)
        card._filtered_items = list(_ITEMS_NO_DESC)
        card._render(incremental=False)
        assert len(card._dividers) == 3

        # 过滤为子集
        card._filtered_items = list(_SUBSET_NO_DESC)
        card._render(incremental=True)
        assert len(card._dividers) == 1, f"子集应有 1 个 divider, 实际 {len(card._dividers)}"
        total = len(card._item_widgets) + len(card._dividers)
        visible = min(total, MAX_VISIBLE_ITEMS)
        expected_h = visible * ITEM_HEIGHT + len(card._dividers)
        assert card.height() == expected_h, f"子集高度 {card.height()} != {expected_h}"

        # 恢复全集
        card._filtered_items = list(_ITEMS_NO_DESC)
        card._render(incremental=True)
        assert len(card._dividers) == 3, f"全集恢复后应有 3 个 dividers, 实际 {len(card._dividers)}"
        total = len(card._item_widgets) + len(card._dividers)
        visible = min(total, MAX_VISIBLE_ITEMS)
        expected_h = visible * ITEM_HEIGHT + len(card._dividers)
        assert card.height() == expected_h, f"全集高度 {card.height()} != {expected_h}"

    def test_apply_list_height_sync(self):
        """_apply_list_height 使用的 divider_count = len(_dividers) 准确"""
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import (
            MAX_VISIBLE_ITEMS,
            ITEM_HEIGHT,
        )

        card = _make_card()
        # 手动设模拟数据
        card._item_widgets = [object() for _ in range(7)]
        card._dividers = [object(), object()]  # 2 dividers

        card._apply_list_height()
        total = 7 + 2
        visible = min(total, MAX_VISIBLE_ITEMS)
        expected = visible * ITEM_HEIGHT + 2
        assert card.height() == expected, f"_apply_list_height 结果 {card.height()} != {expected}"

    def test_apply_list_height_many_items(self):
        """总 items 数 > MAX_VISIBLE_ITEMS 时高度上限正确"""
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import (
            MAX_VISIBLE_ITEMS,
            ITEM_HEIGHT,
        )

        card = _make_card()
        card._item_widgets = [object() for _ in range(50)]
        card._dividers = [object() for _ in range(3)]

        card._apply_list_height()
        expected = MAX_VISIBLE_ITEMS * ITEM_HEIGHT + 3
        assert card.height() == expected, f"多 items 高度 {card.height()} != {expected}"

    def test_short_window_compresses_list_not_tooltip(self):
        """矮窗口下：命令列表进入预算压缩，但悬浮描述气泡仍独立可见

        重构后描述以悬浮气泡呈现，不参与卡片布局高度，因此矮窗口下
        不会被隐藏——列表按预算压缩可见项数量，气泡照常浮在卡片上方。
        回归：窗口很矮时，列表 + 气泡不应挤掉输入框/聊天区，且气泡不占列表空间。
        """
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import (
            MAX_VISIBLE_ITEMS,
            ITEM_HEIGHT,
        )

        card = _card_with_tooltip()  # 600px 窗口，气泡可见，25 items
        # 压扁窗口到 150px，触发预算压缩（测试无 _input_card，预留仅 CARD_RESIZE_RESERVE）
        top = card.window()
        top.resize(600, 150)
        app = QApplication.instance()
        for _ in range(15):
            app.processEvents()

        # 矮窗口下：悬浮气泡仍可见（它浮在卡片上方，独立窗口，不被预算隐藏）
        assert card._desc_tooltip_label.isVisible(), "矮窗口下悬浮气泡仍应可见"

        # 卡片（列表）高度应远小于"完整 8 项"的自然高度（被预算压缩）
        full_natural = MAX_VISIBLE_ITEMS * ITEM_HEIGHT + len(card._dividers)
        assert card.height() < full_natural, f"矮窗口未压缩: card {card.height()} 应 < 自然高度 {full_natural}"
        # 至少仍保留可见项（不为 0）
        assert card.height() >= ITEM_HEIGHT, "矮窗口下至少保留 1 个 item"
