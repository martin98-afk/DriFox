# -*- coding: utf-8 -*-
"""CommandCard 分隔线（divider）与高度计算一致性测试

问题背景：_render 的 incremental=True 路径不清理/不重建 dividers，
导致 _dividers 列表与 scroll_layout 实际状态脱节，_apply_list_height
计算高度时使用 stale divider_count，造成列表高度异常。

修复：_render 全量路径中移除 incremental 守卫，始终清理/重算 dividers。
"""

import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


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


def _widget_for_item(card, item_idx):
    """从虚拟化池中取出绑定到指定 item 索引的 widget（无则返回 None）"""
    for slot, w in card._slot_widgets.items():
        kind, idx, _y = card._virtual_slots[slot]
        if kind == "item" and idx == item_idx:
            return w
    return None


def _card_with_tooltip():
    """创建 CommandCard，让 tooltip 可见（选中第一个有描述的 item）"""
    from PySide6.QtWidgets import QWidget, QVBoxLayout
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
    from PySide6.QtWidgets import QApplication

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
        total = len(card._filtered_items) + card._divider_count
        list_h = min(total, MAX_VISIBLE_ITEMS) * ITEM_HEIGHT + card._divider_count
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

        assert card._divider_count == 3, f"期望 3 个 dividers, 实际 {card._divider_count}"
        total = len(card._filtered_items) + card._divider_count
        visible = min(total, MAX_VISIBLE_ITEMS)
        expected_h = visible * ITEM_HEIGHT + card._divider_count
        assert card.height() == expected_h, f"高度不匹配: {card.height()} != {expected_h}"
        assert len(card._virtual_slots) == total, "虚拟槽数应与 items+dividers 一致"

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

        assert card._divider_count == 3, f"incremental=True 首次: 期望 3 dividers, 实际 {card._divider_count}"
        total = len(card._filtered_items) + card._divider_count
        visible = min(total, MAX_VISIBLE_ITEMS)
        expected_h = visible * ITEM_HEIGHT + card._divider_count
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
        from PySide6.QtWidgets import QWidget, QVBoxLayout

        _parent = QWidget()
        _parent.resize(800, 600)
        _parent.setLayout(QVBoxLayout())
        card = _make_card(items=_ITEMS_NO_DESC)
        _parent.layout().addWidget(card)
        _REF_HOLDER.append(_parent)
        card._filtered_items = list(_ITEMS_NO_DESC)
        card._render(incremental=False)
        pre_div = card._divider_count
        pre_h = card.height()

        card._render(incremental=True)
        assert card._divider_count == pre_div, f"快速路径 dividers 不应变: {pre_div} -> {card._divider_count}"
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
        from PySide6.QtWidgets import QWidget, QVBoxLayout

        _parent = QWidget()
        _parent.resize(800, 600)
        _parent.setLayout(QVBoxLayout())
        card = _make_card(items=_ITEMS_NO_DESC)
        _parent.layout().addWidget(card)
        _REF_HOLDER.append(_parent)
        card._filtered_items = list(_ITEMS_NO_DESC)
        card._render(incremental=False)
        assert card._divider_count == 3

        # 过滤为子集
        card._filtered_items = list(_SUBSET_NO_DESC)
        card._render(incremental=True)
        assert card._divider_count == 1, f"子集应有 1 个 divider, 实际 {card._divider_count}"
        total = len(card._filtered_items) + card._divider_count
        visible = min(total, MAX_VISIBLE_ITEMS)
        expected_h = visible * ITEM_HEIGHT + card._divider_count
        assert card.height() == expected_h, f"子集高度 {card.height()} != {expected_h}"

        # 恢复全集
        card._filtered_items = list(_ITEMS_NO_DESC)
        card._render(incremental=True)
        assert card._divider_count == 3, f"全集恢复后应有 3 个 dividers, 实际 {card._divider_count}"
        total = len(card._filtered_items) + card._divider_count
        visible = min(total, MAX_VISIBLE_ITEMS)
        expected_h = visible * ITEM_HEIGHT + card._divider_count
        assert card.height() == expected_h, f"全集高度 {card.height()} != {expected_h}"

    def test_apply_list_height_sync(self):
        """_apply_list_height 高度口径与 _build_virtual_layout 一致

        真实总高 = item_count * ITEM_HEIGHT + divider_count * 1。
        若用 total_items（含 dividers）作 visible 基数，item_count < MAX_VISIBLE_ITEMS
        时 visible = item_count + divider_count，卡片高度 = 真实高度 + 多余空槽占位，
        列表底部留白。修复后 visible = min(item_count, MAX_VISIBLE_ITEMS)。
        """
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import (
            MAX_VISIBLE_ITEMS,
            ITEM_HEIGHT,
        )

        card = _make_card()
        # 手动设模拟数据（虚拟化后计数源为 _filtered_items + _divider_count）
        card._filtered_items = [{} for _ in range(7)]
        card._divider_count = 2  # 2 dividers

        card._apply_list_height()
        # 真实高度 = 7 * ITEM_HEIGHT + 2 * 1 = 254
        # 旧 bug：visible = min(7 + 2, 8) = 7，算出 7 * 36 + 2 = 254（看似正确），
        # 但当 item_count < MAX_VISIBLE_ITEMS 时（如 5 + 2）会出现 (visible - item_count) * 36 空槽。
        # 修复后：visible = min(item_count, MAX_VISIBLE_ITEMS) = 7，结果与真实高度一致。
        expected = 7 * ITEM_HEIGHT + 2
        assert card.height() == expected, f"_apply_list_height 结果 {card.height()} != {expected}"

    def test_apply_list_height_no_blank_tail_few_items(self):
        """回归：item_count < MAX_VISIBLE_ITEMS + 多 divider 时无底部空槽

        复现 /plugin- 过滤：5 items 跨 3 个 section（UI + skill + prompt）→ 2 dividers。
        旧实现 visible = min(7, 8) = 7，card 高度 = 7*36 + 2 = 254，真实虚拟内容
        高度 5*36 + 2 = 182，底部 72px 空槽。修复后 visible = min(5, 8) = 5，card = 182。
        """
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import (
            MAX_VISIBLE_ITEMS,
            ITEM_HEIGHT,
        )
        from PySide6.QtWidgets import QWidget, QVBoxLayout

        # 放到合理高度的窗口中，避免矮窗口压缩分支掩盖 bug
        _parent = QWidget()
        _parent.resize(800, 600)
        _parent.setLayout(QVBoxLayout())
        card = _make_card()
        _parent.layout().addWidget(card)
        _REF_HOLDER.append(_parent)

        card._filtered_items = [{} for _ in range(5)]
        card._divider_count = 2  # UI/skill/prompt 三个 section → 2 dividers
        card._apply_list_height()

        # 真实虚拟内容高度 = 5 * 36 + 2 = 182
        expected_real = 5 * ITEM_HEIGHT + 2
        # 修复前：visible = min(7, 8) = 7 → 7 * 36 + 2 = 254，比真实多 72px 空槽
        # 修复后：visible = min(5, 8) = 5 → 5 * 36 + 2 = 182，与真实一致
        assert card.height() == expected_real, (
            f"item_count < MAX 时卡片高度 {card.height()} 应 = 真实高度 {expected_real}"
            f"（5 items + 2 dividers = 5*36+2 = 182）"
        )

    def test_apply_list_height_many_items(self):
        """总 items 数 > MAX_VISIBLE_ITEMS 时高度上限正确"""
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import (
            MAX_VISIBLE_ITEMS,
            ITEM_HEIGHT,
        )

        card = _make_card()
        card._filtered_items = [{} for _ in range(50)]
        card._divider_count = 3

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
        full_natural = MAX_VISIBLE_ITEMS * ITEM_HEIGHT + card._divider_count
        assert card.height() < full_natural, f"矮窗口未压缩: card {card.height()} 应 < 自然高度 {full_natural}"
        # 至少仍保留可见项（不为 0）
        assert card.height() >= ITEM_HEIGHT, "矮窗口下至少保留 1 个 item"

    def test_new_item_tag_style_applied_after_show(self):
        """新建（非复用）item 首次显示后，右侧类型标签的级联 QSS 样式应正确应用

        回归：新建 CommandItemWidget 时 _setup_ui 内 _apply_style 在 widget 尚未
        挂载到可见布局时调用，Qt QStyleSheetStyle::repolish 对不可见 widget 跳过，
        级联 QSS（QLabel#tagLabel 颜色/字体）不会应用——过滤变化后新出现项的类型
        标签/快捷键胶囊显示默认样式，hover 后才恢复。
        修复：showEvent 补一次 _apply_style，首次显示即保证样式落地。
        """
        _ensure_qapp()
        from PySide6.QtGui import QColor, QPalette
        from PySide6.QtWidgets import QWidget, QVBoxLayout

        from app.widgets.cards.floating.command_card import CommandCard, _qcolor_from_rgba
        from app.utils.design_tokens import Colors

        Colors.refresh()
        parent = QWidget()
        parent.resize(800, 600)
        parent.setLayout(QVBoxLayout())
        card = CommandCard()
        parent.layout().addWidget(card)
        items = [
            {"name": "cmd-x", "type": "command", "subtype": "", "shortcut": "Ctrl+X", "description": "命令X"},
            {"name": "skill-a", "type": "skill", "description": "技能A"},
            {"name": "agent-b", "type": "agent", "description": "智能体B"},
        ]
        card._all_items_cache = list(items)
        card._cache_dirty = False
        card._refresh_data()
        # 走真实时序：show_card 内部 setVisible(True) 使卡片可见，
        # 新建 widget 挂到可见布局时立即 show → showEvent 补 _apply_style → 级联 QSS 落地
        parent.show()
        card.show_card("")
        app = QApplication.instance()
        for _ in range(10):
            app.processEvents()

        assert len(card._filtered_items) == 3
        w_skill = _widget_for_item(card, 1)
        w_agent = _widget_for_item(card, 2)
        assert w_skill is not None and w_agent is not None, "可见 item 应已绑定池 widget"
        # 首次显示后 palette WindowText 应等于对应类型的标签色（而非默认黑）
        skill_color = w_skill._tag_label.palette().color(QPalette.WindowText)
        agent_color = w_agent._tag_label.palette().color(QPalette.WindowText)
        assert skill_color.name() == _qcolor_from_rgba(Colors.TAG_ACCENT).name(), (
            f"skill tag 应应用 TAG_ACCENT({Colors.TAG_ACCENT})，实际 {skill_color.name()}"
        )
        assert agent_color.name() == _qcolor_from_rgba(Colors.TAG_PURPLE).name(), (
            f"agent tag 应应用 TAG_PURPLE({Colors.TAG_PURPLE})，实际 {agent_color.name()}"
        )

    def test_item_name_quote_rendered_as_rich_text(self):
        """item 名称含引号时 name_label 强制 RichText，&quot; 实体在渲染时解析

        回归：_update_display 对名称 html.escape 后 setText，无高亮分支（不含
        <span>）会被 QLabel AutoText 判定为 PlainText，&quot;/&amp; 实体字面显示
        （与 tooltip &quot; 问题同源）。修复：创建 name_label 时一次设置
        setTextFormat(Qt.RichText)。
        """
        _ensure_qapp()
        import html as html_mod

        from PySide6.QtWidgets import QWidget, QVBoxLayout
        from app.widgets.cards.floating.command_card import CommandCard

        parent = QWidget()
        parent.setLayout(QVBoxLayout())
        parent.resize(800, 600)
        card = CommandCard()
        parent.layout().addWidget(card)
        items = [
            {"name": 'quote"skill', "type": "skill", "description": "技能A"},
            {"name": "cmd-x", "type": "command", "subtype": "", "description": "命令X"},
        ]
        card._all_items_cache = list(items)
        card._cache_dirty = False
        card._refresh_data()
        parent.show()
        card.show_card("")
        app = QApplication.instance()
        for _ in range(10):
            app.processEvents()

        # 排序：command(0) → skill(2)，技能项在 index 1
        assert len(card._filtered_items) == 2
        w = _widget_for_item(card, 1)
        assert w is not None, "可见 item 应已绑定池 widget"
        assert w.item_data["type"] == "skill"
        lbl = w._name_label
        # 强制 RichText：实体在渲染时被解析（此前 PlainText 判定会字面显示 &quot;）
        assert lbl.textFormat() == Qt.RichText, f"name_label 应强制 RichText，实际 {lbl.textFormat()}"
        assert "&quot;" in lbl.text(), "名称应以实体形式存储"
        assert '"' in html_mod.unescape(lbl.text()), "unescape 后应还原为真实引号"
