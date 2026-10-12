# -*- coding: utf-8 -*-
"""置底贴底零露白回归测试（2026-10-12 页脚波动修复）。

根因：跟随态贴底 value 用「预抬上界」（高度回调 setMaximum(+delta) /
`_sync_scroll_maximum` 的 sizeHint 校正，均抢在 Qt 布局传播之前），而
widget 实际高度下一拍布局周期才落地 → value 贴的位置超出真实内容底 →
视口底部露出一帧背景色，流式 16ms tick 每拍重复 = 置底页脚轻微波动。

修复：所有贴底动作改贴 `_landed_bottom_value()`（container.height() -
viewport().height()，与 widget 实际几何同源），预抬上界仅保留给滚动条
range 与 `_is_view_at_bottom` 判定。

探针实证：tests/debug/probe_bottom_stick_flash.py
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QScrollArea, QVBoxLayout, QWidget

from app.main_widget import OpenAIChatToolWindow
from app.widgets.message_card import MessageCard


def _make_win():
    """最小可用宿主：真实滚动区 + 真实方法（照 test_scroll_hot_path 桩模式）"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._is_destroyed = False
    win._programmatic_scroll_depth = 0
    win._scroll_max_cache = None
    win._user_intentionally_away_from_bottom = False
    win._loading_session = False

    scroll_area = QScrollArea()
    scroll_area.setWidgetResizable(True)
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(6, 6, 6, 6)
    layout.setSpacing(6)
    cards = []
    for _ in range(3):
        card = MessageCard(role="assistant", parent=container)
        card.setFixedHeight(200)
        card._content_just_loaded = False
        layout.addWidget(card)
        cards.append(card)
    scroll_area.setWidget(container)
    scroll_area.resize(400, 600)
    scroll_area.show()
    win.chat_scroll_area = scroll_area
    return win, container, cards


def _flash_px(win) -> int:
    """当前贴底露白量：value + viewport 超出 container 实际高度的部分"""
    sb = win.chat_scroll_area.verticalScrollBar()
    return sb.value() + win.chat_scroll_area.viewport().height() - win.chat_scroll_area.widget().height()


def test_pre_lifted_max_actually_flashes(qapp):
    """对照：贴预抬上界确实露白（锁死根因，防止未来改回）"""
    win, _container, cards = _make_win()
    qapp.processEvents()
    sb = win.chat_scroll_area.verticalScrollBar()
    sb.setValue(sb.maximum())
    qapp.processEvents()
    assert _flash_px(win) <= 0

    cards[-1].setFixedHeight(cards[-1].height() + 40)
    sb.setMaximum(sb.maximum() + 40)
    sb.setValue(sb.maximum())  # 修复前行为：贴预抬上界
    assert _flash_px(win) > 0, "前置校验：贴预抬上界必须露白，否则根因认定有误"


def test_follow_bottom_never_flash(qapp):
    """跟随态流式拍：走真实高度回调，贴底落点全程不得超过 widget 实际高"""
    win, _container, cards = _make_win()
    qapp.processEvents()
    card = cards[-1]
    win.sender = lambda: card  # type: ignore[method-assign]

    sb = win.chat_scroll_area.verticalScrollBar()
    win._sync_scroll_maximum()
    sb.setValue(sb.maximum())
    qapp.processEvents()
    assert _flash_px(win) <= 0

    # 模拟 3 个流式 tick：增高 + delta 记账 + 真实高度回调（跟随态）。
    # 刻意不先 processEvents：回调正运行在「预抬与布局落地之间」的窗口。
    for _ in range(3):
        card.setFixedHeight(card.height() + 40)
        card._last_height_delta = 40
        win._on_message_card_height_changed(card.height())
        assert _flash_px(win) <= 0, "贴底落点不得超过 widget 实际高（露白即波动）"
        qapp.processEvents()  # 布局落地
        assert _flash_px(win) <= 0

    # 落地后追平到底（锚定拍/重试链职责）：最终仍要贴住真实底部
    win._sync_scroll_maximum()
    sb.setValue(win._landed_bottom_value())
    qapp.processEvents()
    assert sb.maximum() - sb.value() <= 24, "布局落地后必须追平到底（跟随语义不能丢）"


def test_anchor_compensation_clamped_to_landed(qapp):
    """非跟随态锚定补偿落点同样钳到已落地底，不落在预抬区间"""
    win, _container, cards = _make_win()
    # 首卡矮（补偿分支要求卡片整体在视口上方），后两卡撑出足够滚动余量，
    # 使 value=100 时真实离底（否则 away 兜底判定按实际位置翻回跟随态）
    cards[0].setFixedHeight(80)
    cards[1].setFixedHeight(800)
    cards[2].setFixedHeight(800)
    qapp.processEvents()
    card = cards[0]
    win.sender = lambda: card  # type: ignore[method-assign]

    sb = win.chat_scroll_area.verticalScrollBar()
    sb.setValue(100)  # 离底 → away 兜底判定生效
    win._user_intentionally_away_from_bottom = True
    qapp.processEvents()
    assert not win._should_follow_bottom()

    card.setFixedHeight(card.height() + 500)
    card._last_height_delta = 500
    win._on_message_card_height_changed(card.height())
    assert _flash_px(win) <= 0, "锚定补偿落点不得落在预抬区间（露白即波动）"


def test_landed_bottom_value_semantics(qapp):
    """_landed_bottom_value：等于已落地内容底，且永不越过滚动条上界"""
    win, container, _cards = _make_win()
    qapp.processEvents()
    sb = win.chat_scroll_area.verticalScrollBar()
    landed = win._landed_bottom_value()
    assert landed == max(0, min(sb.maximum(), container.height() - win.chat_scroll_area.viewport().height()))
    assert landed <= sb.maximum()
