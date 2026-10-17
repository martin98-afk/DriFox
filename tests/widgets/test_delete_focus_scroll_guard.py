# -*- coding: utf-8 -*-
"""回归测试：删除卡片时的焦点自动滚动守卫（视口跳变）。

## 用户可见现象

长会话滚轮上下滚动、虚拟滚动回收卡片时，视口突然跳走、不保持原位。
触发条件：**焦点落在这张即将被删除的卡片内部**（用户点过卡内按钮，
如页脚的复制/重试按钮、正文区）。

## 根因链（2026-10-14 探针实证，tests/debug/_tmp_probe_scroll_entry.py）

1. 焦点在卡片子树内（`QApplication.focusWidget()` 是卡内控件）
2. `delete_widgets_from_layout` 调 `widget.hide()`
3. Qt 检测到被隐藏的子树含焦点 → `focusNextPrevChild` 把焦点移给
   **下一个可聚焦控件**（可能是滚动区外的控件）
4. 该控件在滚动区内时，QScrollArea 随即 `scrollContentsBy` 把视口滚到它旁边
   实测 value 8602 → 388（跳 8214px）—— 用户看到的就是「滚轮位置乱跳」

## 修复

`ui_helpers._detach_focus_from_widgets()`：删除前把焦点从待删子树中
`clearFocus()`，隐藏过程不再触发 Qt 的自动转移滚动。
`delete_widgets_from_layout` / `_clear_chat_area` / `_take_chat_widgets` /
`MessageCard.detach_viewer` 四条路径统一补齐。
"""

import sys

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget

from app.widgets.ui_helpers import _detach_focus_from_widgets, delete_widgets_from_layout

CARD_H = 300
VIEWPORT_H = 400
N_CARDS = 20


class _FocusableCard(QWidget):
    """卡内带一个可聚焦按钮（模拟 MessageCard 页脚按钮）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(CARD_H)
        self._btn = _FocusableButton(self)


class _FocusableButton(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFixedHeight(20)


def _build_scroll_area(qapp):
    """QScrollArea + 20 张卡（每张含可聚焦按钮），返回 (area, container, layout, cards)。"""
    area = QScrollArea()
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    area.setWidget(container)
    area.setWidgetResizable(True)
    area.resize(500, VIEWPORT_H)

    cards = []
    for _ in range(N_CARDS):
        card = _FocusableCard(container)
        layout.addWidget(card)
        cards.append(card)

    area.show()
    for _ in range(3):
        qapp.processEvents()
    container.adjustSize()
    for _ in range(3):
        qapp.processEvents()
    return area, container, layout, cards


def _focus_in_card(card, qapp) -> QWidget:
    """把焦点放进卡片内的按钮，返回该按钮。"""
    btn = card._btn
    btn.setFocus(Qt.OtherFocusReason)
    for _ in range(2):
        qapp.processEvents()
    return btn


def test_focused_card_delete_keeps_viewport(qapp):
    """焦点在被删卡内时，删除后视口 value 必须保持原位（核心断言）。

    未修复实测：滚到中段 value=3060 → 489（drift -2571px）。
    """
    area, container, layout, cards = _build_scroll_area(qapp)
    sb = area.verticalScrollBar()

    # 焦点放进卡 0（用户点过卡内按钮）
    _focus_in_card(cards[0], qapp)

    # 滚到中段（卡 0 在视口上方，将被回收）
    sb.setValue(10 * CARD_H)
    for _ in range(3):
        qapp.processEvents()
    value_before = sb.value()

    # 走真实删除入口：删除卡 0
    delete_widgets_from_layout([cards[0]], layout)

    for _ in range(4):
        qapp.processEvents()
    drift = sb.value() - value_before
    assert abs(drift) <= 2, (
        f"删除含焦点卡片后视口漂移 {drift}px（{value_before} → {sb.value()}）。"
        f"根因：hide() 触发 Qt 焦点链自动转移 + scrollContentsBy 自动滚动，"
        f"修复见 _detach_focus_from_widgets"
    )


def test_delete_without_focus_is_unchanged(qapp):
    """焦点不在待删卡内时，删除行为不受影响（不误伤）。

    对照组：焦点在**保留**卡片内，删除另一张卡 → 视口与焦点都必须保持。
    """
    area, container, layout, cards = _build_scroll_area(qapp)
    sb = area.verticalScrollBar()

    btn = _focus_in_card(cards[10], qapp)
    sb.setValue(10 * CARD_H)
    for _ in range(3):
        qapp.processEvents()
    value_before = sb.value()

    delete_widgets_from_layout([cards[0]], layout)

    for _ in range(4):
        qapp.processEvents()
    assert abs(sb.value() - value_before) <= 2, (
        f"焦点不在待删卡内时视口不应移动：{value_before} → {sb.value()}"
    )
    assert qapp.focusWidget() is btn, "保留卡的焦点不得被摘除（误伤检查）"


def test_detach_focus_only_when_inside_targets(qapp):
    """`_detach_focus_from_widgets` 只在焦点确实位于待删子树内时才清焦点。"""
    area, container, layout, cards = _build_scroll_area(qapp)

    # 焦点在卡 5 内，待删列表是卡 0 → 不应清
    btn5 = _focus_in_card(cards[5], qapp)
    _detach_focus_from_widgets([cards[0]])
    assert qapp.focusWidget() is btn5, "焦点不在待删子树内，不得被清除"

    # 待删列表包含卡 5 → 应清
    _detach_focus_from_widgets([cards[0], cards[5]])
    assert qapp.focusWidget() is not btn5, "焦点位于待删子树内时，必须清除"


def test_detach_focus_handles_dead_widgets(qapp):
    """sip 已删除的 widget 传入时不得抛异常（防御性）。"""
    area, container, layout, cards = _build_scroll_area(qapp)
    _focus_in_card(cards[0], qapp)

    card = cards[1]
    layout.removeWidget(card)
    card.setParent(None)
    card.deleteLater()
    for _ in range(3):
        qapp.processEvents()

    # 不应抛异常（RuntimeError 被吞）
    _detach_focus_from_widgets([cards[0], card])


def test_source_wiring_all_delete_paths():
    """源码断言：四条删除路径都接入了焦点摘除。

    单点修复容易漏路径（回收 / 清空会话 / 切会话缓存 / viewer 池化 detach
    是四个不同入口），用源码断言锁全部接线，避免未来某条路径回归。
    """
    with open("app/widgets/ui_helpers.py", encoding="utf-8") as f:
        helper_src = f.read()
    assert "_detach_focus_from_widgets(widgets_to_remove)" in helper_src, (
        "delete_widgets_from_layout 必须调用 _detach_focus_from_widgets"
    )

    with open("app/main_widget.py", encoding="utf-8") as f:
        main_src = f.read()
    assert "_detach_focus_from_widgets(widgets)" in main_src, (
        "_clear_chat_area 必须调用 _detach_focus_from_widgets"
    )
    assert "_detach_focus_from_widgets([w])" in main_src, (
        "_take_chat_widgets 必须调用 _detach_focus_from_widgets"
    )

    with open("app/widgets/message_card.py", encoding="utf-8") as f:
        card_src = f.read()
    start = card_src.index("def detach_viewer(")
    body = card_src[start : card_src.index("def ensure_rendered", start)]
    assert "_detach_focus_from_widgets" in body, (
        "MessageCard.detach_viewer 摘 viewer 前必须摘焦点（焦点在 WebEngine delegate 时同源隐患）"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
