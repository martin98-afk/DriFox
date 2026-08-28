# -*- coding: utf-8 -*-
"""CommandCard 虚拟化渲染回归测试

背景：命令/技能/智能体总数多时（100+ 项），旧实现为所有匹配项创建 widget
（每项含 4 个 QLabel + 样式表），每次敲键全量重建数百 widget 导致卡顿。
虚拟化后只创建/绑定可见窗口（含缓冲）内的池 widget，滚动复用池绑定数据，
内容再多也只渲染十几个 widget。

本测试锁定虚拟化的核心不变量：
1. 池 widget 数量有上限，不随匹配项总数增长
2. 滚动后绑定槽始终落在可见窗口（含缓冲）范围内
3. 选中导航会滚动列表使选中项可见
4. 虚拟布局槽的 y 偏移 = items（36px）+ 分隔线（1px）累加
"""

import sys

from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget


def _ensure_qapp():
    """确保 QApplication 可用"""
    return QApplication.instance() or QApplication(sys.argv)


def _make_items(count=200):
    """构造大量分区的 items（command + skill + agent → 2 个分隔线）"""
    items = []
    for i in range(count // 2):
        items.append({"name": f"cmd{i:03d}", "type": "command", "subtype": "", "description": ""})
    for i in range(count // 3):
        items.append({"name": f"skill{i:03d}", "type": "skill", "description": ""})
    for i in range(count - len(items)):
        items.append({"name": f"agent{i:03d}", "type": "agent", "description": ""})
    return items


def _make_card(items):
    """创建挂载到可见窗口中的 CommandCard 并渲染"""
    from app.widgets.cards.floating.command_card import CommandCard

    parent = QWidget()
    parent.resize(800, 600)
    parent.setLayout(QVBoxLayout())
    card = CommandCard()
    parent.layout().addWidget(card)
    card._all_items_cache = list(items)
    card._cache_dirty = False
    card._refresh_data()
    parent.show()
    card.show_card("")
    app = QApplication.instance()
    for _ in range(10):
        app.processEvents()
    _REF_HOLDER.append(parent)
    return card


_REF_HOLDER = []  # 防止局部 parent 被 GC 回收


class TestVirtualization:
    def test_pool_size_bounded(self):
        """大量 items 渲染后池 widget 数量有上限（不随内容总数增长）"""
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import VIRTUAL_POOL_SIZE

        items = _make_items(300)
        card = _make_card(items)
        assert len(card._filtered_items) == len(items), "全部匹配项应进入 filtered"
        assert len(card._item_pool) <= VIRTUAL_POOL_SIZE, (
            f"池 widget {len(card._item_pool)} 应 <= 上限 {VIRTUAL_POOL_SIZE}"
        )
        # 可见槽数也应受池限制
        assert len(card._slot_widgets) <= VIRTUAL_POOL_SIZE

    def test_virtual_layout_offsets(self):
        """虚拟布局槽 y 偏移 = items(36px) + 分隔线(1px) 正确累加"""
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import ITEM_HEIGHT

        items = _make_items(200)
        card = _make_card(items)
        # 2 个分区边界 → 2 个分隔线
        assert card._divider_count == 2, f"期望 2 个分隔线, 实际 {card._divider_count}"
        slots = card._virtual_slots
        assert len(slots) == len(items) + 2
        # y 单调递增且 item 槽间距 = ITEM_HEIGHT
        for i in range(1, len(slots)):
            assert slots[i][2] > slots[i - 1][2], "槽 y 应单调递增"
        # 内容总高度 = 最后槽 y + 最后槽高度
        last_kind, _idx, last_y = slots[-1]
        expected_total = last_y + (ITEM_HEIGHT if last_kind == "item" else 1)
        assert card._virtual_total_height == expected_total

    def test_scroll_binds_visible_window(self):
        """滚动后绑定槽始终在可见窗口（含缓冲）范围内"""
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import VIRTUAL_BUFFER_SLOTS

        items = _make_items(200)
        card = _make_card(items)
        sb = card._scroll_area.verticalScrollBar()
        assert sb.maximum() > 0, "内容多时应可滚动"
        # 滚到中部
        sb.setValue(sb.maximum() // 2)
        app = QApplication.instance()
        for _ in range(10):
            app.processEvents()
        view_h = card._scroll_area.viewport().height()
        y0 = sb.value()
        y1 = y0 + view_h
        for slot in card._slot_widgets:
            kind, _idx, y = card._virtual_slots[slot]
            slot_h = 1 if kind == "divider" else 36
            # 允许缓冲提前量：槽在窗口上下 VIRTUAL_BUFFER_SLOTS*ITEM_HEIGHT 内
            assert y + slot_h + VIRTUAL_BUFFER_SLOTS * 36 >= y0, f"槽 {slot} 上越界"
            assert y <= y1 + VIRTUAL_BUFFER_SLOTS * 36, f"槽 {slot} 下越界"
        # 滚到底部
        sb.setValue(sb.maximum())
        for _ in range(10):
            app.processEvents()
        # 底部可见槽应包含最后一个 item
        last_slot = len(card._virtual_slots) - 1
        assert last_slot in card._slot_widgets, "底部滚动后最后槽应已绑定"

    def test_selection_navigates_and_scrolls(self):
        """select_next 连续导航超过一屏后滚动跟随，选中项始终可见"""
        _ensure_qapp()
        items = _make_items(200)
        card = _make_card(items)
        sb = card._scroll_area.verticalScrollBar()
        # 连按 20 次下一项（超过一屏 8 项）
        for _ in range(20):
            card.select_next()
        app = QApplication.instance()
        for _ in range(10):
            app.processEvents()
        assert card._selected_index == 20
        view_h = card._scroll_area.viewport().height()
        y0 = sb.value()
        # 选中项应完全可见（或被滚动到视口内）
        for kind, idx, y in card._virtual_slots:
            if kind == "item" and idx == card._selected_index:
                assert y >= y0 - 1 and y + 36 <= y0 + view_h + 1, (
                    f"选中项 y={y} 不在视口 [{y0}, {y0 + view_h}]"
                )
                break
        else:
            raise AssertionError("未找到选中项对应的虚拟槽")

    def test_filter_reduces_and_restores(self):
        """过滤为子集再恢复，池复用且绑定正确"""
        _ensure_qapp()
        from app.widgets.cards.floating.command_card import VIRTUAL_POOL_SIZE

        items = _make_items(200)
        card = _make_card(items)
        pool_before = list(card._item_pool)
        # 过滤到 3 项
        card._filtered_items = items[:3]
        card._render(incremental=False)
        assert len(card._slot_widgets) == 3, "3 项全部可见应全部绑定"
        # 恢复全集：池应复用（不新建）
        card._filtered_items = items
        card._render(incremental=False)
        assert len(card._item_pool) <= VIRTUAL_POOL_SIZE
        assert len(card._slot_widgets) <= VIRTUAL_POOL_SIZE
        assert len(card._virtual_slots) == len(items) + 2

    def test_clicked_and_hovered_mapping(self):
        """点击/hover 池 widget 能映射回正确的 item 索引"""
        _ensure_qapp()
        items = _make_items(60)
        card = _make_card(items)
        # 取第一个绑定 widget（应为 item 0）
        for slot, w in card._slot_widgets.items():
            kind, idx, _y = card._virtual_slots[slot]
            if kind == "item" and idx == 0:
                card._selected_index = -1
                card._last_selected_index = -1
                w.clicked.emit()
                assert card._selected_index == 0, "点击第一个 item 应选中 index 0"
                # hover 第 3 个 item
                for slot2, w2 in card._slot_widgets.items():
                    kind2, idx2, _y2 = card._virtual_slots[slot2]
                    if kind2 == "item" and idx2 == 2:
                        card._on_item_hovered(w2)
                        assert card._selected_index == 2, "hover 第 3 个 item 应选中 index 2"
                        return
        raise AssertionError("未找到绑定 item 0 的池 widget")

    def test_width_sync_after_layout(self):
        """首帧布局前绑定的 widget 宽度应为 0/初始值，布局完成后同步为内容宽度

        回归：首次 show_card 发生在卡片布局完成前（viewport 高度 0 → 只绑定
        顶部几槽），此时 content 宽度未确定，绑定 widget 被 setFixedWidth(0)
        挤压（tag 标签挤到左边）；布局完成后 content 变宽，已绑定 widget 必须
        同步更新宽度，否则滚动重绑前一直错位。
        """
        _ensure_qapp()
        from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout

        from app.widgets.cards.floating.command_card import CommandCard, CommandItemWidget

        parent = QWidget()
        parent.resize(800, 600)
        parent.setLayout(QVBoxLayout())
        card = CommandCard()
        parent.layout().addWidget(card)
        items = _make_items(30)
        card._all_items_cache = list(items)
        card._cache_dirty = False
        card._refresh_data()
        card._filtered_items = list(items)
        # 父窗口未显示：content 宽度尚未确定 → 首帧渲染
        card._render(incremental=False)
        app = QApplication.instance()
        for _ in range(5):
            app.processEvents()
        assert len(card._slot_widgets) > 0, "首帧应已绑定槽"
        # 布局完成后：content 宽度应 > 0，已绑定 widget 宽度同步为内容宽度
        parent.show()
        for _ in range(15):
            app.processEvents()
        content_w = card._scroll_content.width()
        assert content_w > 0, "布局后内容宽度应 > 0"
        for slot, w in card._slot_widgets.items():
            if not isinstance(w, CommandItemWidget):
                continue
            assert w.width() == content_w, (
                f"槽 {slot} 宽度 {w.width()} 应同步为内容宽度 {content_w}"
            )
        _REF_HOLDER.append(parent)
