# -*- coding: utf-8 -*-
"""探针 AG：真实时序补偿矩阵 v2（每场景独立环境，用完即删）。

场景（非跟随态，用户在中段阅读）：
  1. 卡0 完全在视口上方，长高 400 → 期望补偿 +400
  2. 卡0 跨视口顶（视口顶在卡0 内部），长高 400 → 期望补偿 +400（关键！保持视口顶内容不动）
  3. 卡2 完全在视口下方，长高 400 → 期望 0
"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PySide6.QtCore import QObject, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication([])

from app.main_widget import OpenAIChatToolWindow  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

VIEWPORT_H = 400


def p(*a):
    print(*a)
    sys.stdout.flush()


class Harness(QObject):
    _on_message_card_height_changed = OpenAIChatToolWindow._on_message_card_height_changed
    _should_follow_bottom = OpenAIChatToolWindow._should_follow_bottom
    _is_view_at_bottom = OpenAIChatToolWindow._is_view_at_bottom
    _programmatic_scroll = OpenAIChatToolWindow._programmatic_scroll
    _landed_bottom_value = OpenAIChatToolWindow._landed_bottom_value


def build(card_h=340, n=10):
    area = QScrollArea()
    container = QWidget()
    lay = QVBoxLayout(container)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    area.setWidget(container)
    area.setWidgetResizable(True)
    area.resize(500, VIEWPORT_H)

    cards = []
    for i in range(n):
        card = MessageCard(role="assistant", parent=container)
        card._message_index = i
        card._content_just_loaded = False
        fv = QWidget(card)
        fv.setFixedHeight(card_h)
        try:
            card._viewer_layout.addWidget(fv)
        except Exception:
            card.layout().addWidget(fv)
        card.viewer = fv
        lay.addWidget(card)
        cards.append(card)
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


def case(tag, *, target, value_before, grow, expect):
    win, area, container, cards = build()
    sb = area.verticalScrollBar()
    sb.setValue(value_before)
    for _ in range(3):
        app.processEvents()

    card = cards[target]
    old_h = card.height()
    old_top = card.mapTo(container, card.rect().topLeft()).y()
    old_bottom = old_top + old_h
    v_before = sb.value()

    # 真实路径：viewer 高度变化 → heightChanged
    card._apply_viewer_height(old_h + grow)
    h_at_emit = card.height()
    p(f'    (调试) viewer._last_applied={getattr(card, "_last_applied_viewer_height", None)} delta={getattr(card, "_last_height_delta", None)} _commit 分支 batch={getattr(card, "_height_batch", None)}')
    for _ in range(5):
        app.processEvents()
    after_h = card.height()
    after_bottom = card.mapTo(container, card.rect().topLeft()).y() + after_h
    actual = sb.value() - v_before

    mark = "✅" if actual == expect else f"❌ 期望 {expect:+d}"
    p(f"[{tag}]")
    p(f"    value={v_before} 卡{target} top={old_top} h {old_h}->{after_h} bottom {old_bottom}->{after_bottom}")
    p(f"    emit 时 card.h={h_at_emit}（布局前） 实测补偿={actual:+d}  {mark}")


# 场景 1: 卡0 完全在视口上方（卡底 340 < value 1200）
case("场景1 全上方", target=0, value_before=1200, grow=400, expect=400)

# 场景 2: 卡0 跨视口顶（视口顶 value=100 在卡0 内部）
case("场景2 跨视口顶", target=0, value_before=100, grow=400, expect=400)

# 场景 3: 卡2 完全在视口下方（value=100，卡2 y=680）
case("场景3 全下方", target=2, value_before=100, grow=400, expect=0)

sys.stdout.flush()
os._exit(0)
