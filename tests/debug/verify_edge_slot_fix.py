# -*- coding: utf-8 -*-
"""[DEBUG-hvsc] 命令卡片视口底半行 item 修复验证脚本

对应回归测试：tests/widgets/test_command_card_hover_edge_slot.py
（该环境下 pytest 跑 widgets 用例不稳定，用本脚本做等价断言）

验证点：
1. 16 项 4 区：卡片高度 = 第 8 个 item 槽结束 y（不再计入视口外分隔线）
2. 视口底无「半行 item」：顶边可见的 item 必须完整可见
3. hover 视口内最后一项不触发滚动
4. 9 项 2 区：第 9 项完全不可见，卡片高度 = 288
5. 矮窗口压缩分支仍工作（高度随预算收缩）
"""

import faulthandler
import os
import sys

faulthandler.enable()
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget

_ITEMS_16_FOUR_SECTIONS = (
    [{"name": f"cmd{i}", "type": "command", "subtype": "", "description": ""} for i in range(7)]
    + [{"name": "ui0", "type": "command", "subtype": "ui_plugin", "description": ""}]
    + [{"name": f"skill{i}", "type": "skill", "description": ""} for i in range(5)]
    + [{"name": f"agent{i}", "type": "agent", "description": ""} for i in range(3)]
)
_ITEMS_9_TWO_SECTIONS = [
    {"name": f"cmd{i}", "type": "command", "subtype": "", "description": ""} for i in range(8)
] + [{"name": "skill0", "type": "skill", "description": ""}]


def log(m):
    print(m, flush=True)


def build(app, items):
    from app.widgets.cards.card_container import BottomCardContainer
    from app.widgets.cards.floating.command_card import CommandCard

    parent = QWidget()
    parent.setLayout(QVBoxLayout())
    parent.resize(800, 600)
    container = BottomCardContainer()
    parent.layout().addWidget(container)
    card = CommandCard()
    container.add_card("probe", card)
    card._all_items_cache = list(items)
    card._cache_dirty = False
    card._filtered_items = list(items)
    card._render(incremental=False)
    parent.show()
    card.setVisible(True)
    for _ in range(10):
        app.processEvents()
    return parent, card


def nth_item_end_y(card, n):
    count = 0
    for kind, _idx, y in card._virtual_slots:
        if kind != "item":
            continue
        count += 1
        if count == n:
            return y + 36
    return None


def visible_item_slots(card, item_cls):
    view_h = card._scroll_area.viewport().height()
    out = []
    for slot in sorted(card._slot_widgets.keys()):
        kind, idx, y = card._virtual_slots[slot]
        w = card._slot_widgets[slot]
        if kind != "item" or idx is None:
            continue
        if not isinstance(w, item_cls) or not w.isVisible() or y >= view_h:
            continue
        out.append((idx, y))
    return out


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    from app.widgets.cards.floating.command_card import ITEM_HEIGHT, CommandItemWidget

    ok = True

    # ── 场景 1：16 项 4 区 ──
    parent, card = build(app, _ITEMS_16_FOUR_SECTIONS)
    view_h = card._scroll_area.viewport().height()
    sb = card._scroll_area.verticalScrollBar()
    log(f"[16项4区] divider={card._divider_count} card_h={card.height()} view_h={view_h}")
    expected = nth_item_end_y(card, 8)
    if card.height() != expected:
        ok = False
        log(f"  FAIL: 卡片高度 {card.height()} != 第 8 个 item 槽结束 y {expected}")
    else:
        log(f"  PASS: 卡片高度 = 第 8 个 item 槽结束 y = {expected}")
    partial = [(i, y) for i, y in visible_item_slots(card, CommandItemWidget) if y + ITEM_HEIGHT > view_h]
    if partial:
        ok = False
        log(f"  FAIL: 存在半行 item: {partial}")
    else:
        log("  PASS: 视口内所有 item 完整可见（无半行）")
    slots = visible_item_slots(card, CommandItemWidget)
    last_idx = max(i for i, _y in slots)
    for slot in sorted(card._slot_widgets.keys()):
        kind, idx, _y = card._virtual_slots[slot]
        if kind == "item" and idx == last_idx:
            card._on_item_hovered(card._slot_widgets[slot])
            for _ in range(3):
                app.processEvents()
            break
    if sb.value() != 0:
        ok = False
        log(f"  FAIL: hover 视口内最后一项后 sb.value={sb.value()}（应 0）")
    else:
        log("  PASS: hover 视口内最后一项不滚动")
    parent.hide()
    del parent, card

    # ── 场景 2：9 项 2 区 ──
    parent, card = build(app, _ITEMS_9_TWO_SECTIONS)
    view_h = card._scroll_area.viewport().height()
    log(f"[9项2区] divider={card._divider_count} card_h={card.height()} view_h={view_h}")
    if card.height() != 8 * ITEM_HEIGHT:
        ok = False
        log(f"  FAIL: 卡片高度 {card.height()} != 288")
    else:
        log("  PASS: 卡片高度 = 288（视口外分隔线不再计入）")
    edge = [(i, y) for i, y in visible_item_slots(card, CommandItemWidget) if y + ITEM_HEIGHT > view_h]
    if edge:
        ok = False
        log(f"  FAIL: 存在半行 item: {edge}")
    else:
        log("  PASS: 第 9 项完全不可见（无露头）")
    parent.hide()
    del parent, card

    # ── 场景 3：矮窗口压缩分支仍工作 ──
    parent, card = build(app, _ITEMS_16_FOUR_SECTIONS)
    top = card.window()
    top.resize(600, 260)
    for _ in range(15):
        app.processEvents()
    log(f"[矮窗口260] card_h={card.height()} budget 下压缩触发={card.height() < 288}")
    if card.height() >= 288:
        ok = False
        log("  FAIL: 矮窗口未压缩")
    else:
        log("  PASS: 矮窗口下高度收缩")
    parent.hide()
    del parent, card

    log("\nALL PASS" if ok else "\nSOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
