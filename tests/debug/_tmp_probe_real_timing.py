# -*- coding: utf-8 -*-
"""探针 AF：真实时序补偿判据矩阵（用完即删）。

真实 MessageCard + 轻量 viewer（QWidget 代餐）：
- 卡片高度靠布局自适应（不是 setFixedHeight）
- viewer.setFixedHeight 后 emit heightChanged → 此时 card.height() 还是旧值（真实时序！）
"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PyQt5.QtCore import QObject, Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication([])

from app.main_widget import OpenAIChatToolWindow  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

VIEWPORT_H = 400
K = 0  # 卡片固定开销（这里简易）

CALLS = []


def p(*a):
    print(*a)
    sys.stdout.flush()


class Harness(QObject):
    _on_message_card_height_changed = OpenAIChatToolWindow._on_message_card_height_changed
    _should_follow_bottom = OpenAIChatToolWindow._should_follow_bottom
    _is_view_at_bottom = OpenAIChatToolWindow._is_view_at_bottom
    _programmatic_scroll = OpenAIChatToolWindow._programmatic_scroll
    _landed_bottom_value = OpenAIChatToolWindow._landed_bottom_value


def make_card(container):
    """构造带轻量 viewer 的真实 MessageCard。"""
    card = MessageCard(role="assistant", parent=container)
    card._message_index = 0
    card._content_just_loaded = False
    fake_viewer = QWidget(card)
    fake_viewer.setFixedHeight(200)
    # 塞进卡片的 viewer 布局
    try:
        card._viewer_layout.addWidget(fake_viewer)
    except Exception:
        lay = card.layout()
        if lay is not None:
            lay.addWidget(fake_viewer)
    card.viewer = fake_viewer
    return card


def build():
    area = QScrollArea()
    container = QWidget()
    lay = QVBoxLayout(container)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    area.setWidget(container)
    area.setWidgetResizable(True)
    area.resize(500, VIEWPORT_H)

    cards = []
    for i in range(10):
        c = make_card(container)
        lay.addWidget(c)
        cards.append(c)
    area.show()
    for _ in range(4):
        app.processEvents()
    container.adjustSize()
    for _ in range(4):
        app.processEvents()

    win = Harness()
    win.chat_scroll_area = area
    win.chat_container = container
    win.chat_layout = lay
    win._programmatic_scroll_depth = 0
    win._user_intentionally_away_from_bottom = True
    win._is_streaming = False
    win._scroll_max_cache = None
    for c in cards:
        c.heightChanged.connect(win._on_message_card_height_changed)
    return win, area, container, cards


win, area, container, cards = build()
sb = area.verticalScrollBar()
heights = [c.height() for c in cards]
tops = [c.mapTo(container, c.rect().topLeft()).y() for c in cards]
p(f"环境: max={sb.maximum()} 卡高={heights} tops={tops}")


def case(tag, *, target, value_before, viewer_to, expect_sign=None):
    CALLS.clear()
    sb.setValue(value_before)
    for _ in range(3):
        app.processEvents()
    card = cards[target]
    old_h = card.height()
    old_top = card.mapTo(container, card.rect().topLeft()).y()
    old_bottom = old_top + old_h

    v_before = sb.value()
    # 真实路径：调 _apply_viewer_height（内部走 _commit_viewer_height → heightChanged.emit）
    card._apply_viewer_height(viewer_to)
    # emit 时刻的 cards 高度（在 processEvents 前）
    h_at_emit = card.height()
    for _ in range(3):
        app.processEvents()
    new_bottom = card.mapTo(container, card.rect().topLeft()).y() + card.height()
    actual = sb.value() - v_before

    p(f"[{tag}]")
    p(f"    value={v_before} 卡{target} top={old_top} h {old_h}->{card.height()} bottom {old_bottom}->{new_bottom}")
    p(f"    emit 时 card.h={h_at_emit}（布局前）  viewer={viewer_to}")
    p(f"    实测补偿={actual:+d}")


# 场景 1：卡0 在视口上方完全（value=2000 远大于卡0底）
case("场景1 全上方", target=0, value_before=2000, viewer_to=600)

# 场景 2：卡0 跨视口顶（value=100 在卡0 内部）
case("场景2 跨视口顶", target=0, value_before=100, viewer_to=600)

# 场景 3：prepend 场景——视口在内容最顶（value=0），卡0 长高
case("场景3 value=0 顶部", target=0, value_before=0, viewer_to=600)

sys.stdout.flush()
os._exit(0)
