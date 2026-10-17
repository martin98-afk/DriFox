# -*- coding: utf-8 -*-
"""探针 AE：`_on_message_card_height_changed` 补偿判据矩阵（用完即删）。

用 _Harness(QObject) 载体（同 vscroll_rebuild_delta_repro.py 手法）。

场景：用户在中段阅读（非跟随态）。卡片顶固定、底部长高。
被测判据：`card_bottom <= value` 才补偿。

case 1: 卡完全在视口上方（new_bottom <= value）→ 期望补偿 +delta
case 2: 卡跨视口顶（old_bottom <= value < new_bottom）→ 期望补偿 +delta（关键待验）
case 3: 卡完全在视口下方（old_bottom > value）→ 期望不补偿
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
    """承载 main_widget 真实方法的轻量载体。"""

    _on_message_card_height_changed = OpenAIChatToolWindow._on_message_card_height_changed
    _should_follow_bottom = OpenAIChatToolWindow._should_follow_bottom
    _is_view_at_bottom = OpenAIChatToolWindow._is_view_at_bottom
    _programmatic_scroll = OpenAIChatToolWindow._programmatic_scroll
    _landed_bottom_value = OpenAIChatToolWindow._landed_bottom_value





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
        c = MessageCard(role="assistant", parent=container)
        c.setFixedHeight(300)
        c._message_index = i
        c._content_just_loaded = False
        lay.addWidget(c)
        cards.append(c)
    area.show()
    for _ in range(3):
        app.processEvents()
    container.adjustSize()
    for _ in range(3):
        app.processEvents()

    win = Harness()
    win.chat_scroll_area = area
    win.chat_container = container
    win.chat_layout = lay
    win._programmatic_scroll_depth = 0
    win._user_intentionally_away_from_bottom = True  # 非跟随态
    win._is_streaming = False
    win._scroll_max_cache = None
    for c in cards:
        c.heightChanged.connect(win._on_message_card_height_changed)
    return win, area, container, cards


def case(tag, win, area, container, cards, *, target, value_before, grow_to, expect):
    sb = area.verticalScrollBar()
    sb.setValue(value_before)
    for _ in range(3):
        app.processEvents()

    card = cards[target]
    old_h = card.height()
    old_top = card.mapTo(container, card.rect().topLeft()).y()
    old_bottom = old_top + old_h
    delta = grow_to - old_h

    card.setFixedHeight(grow_to)
    for _ in range(3):
        app.processEvents()
    new_bottom = card.mapTo(container, card.rect().topLeft()).y() + card.height()

    v_before = sb.value()
    card._last_height_delta = delta
    card.heightChanged.emit(grow_to)
    for _ in range(2):
        app.processEvents()
    actual = sb.value() - v_before
    mark = "✅" if actual == expect else f"❌ 期望 {expect:+d}"
    p(f"[{tag}]")
    p(f"    value={value_before} 卡{target} y={old_top} h {old_h}->{grow_to} bottom {old_bottom}->{new_bottom}")
    p(f"    delta={delta:+d}  实测补偿={actual:+d}  {mark}")

    # 复位
    card.setFixedHeight(300)
    for _ in range(3):
        app.processEvents()


win, area, container, cards = build()
sb = area.verticalScrollBar()
p(f"环境: max={sb.maximum()} 卡 300×10")

# case 1: 卡0（y=0..300）完全在视口上方，value=500
case("case1 全上方", win, area, container, cards, target=0, value_before=500, grow_to=400, expect=100)

# case 2: 卡0 从 300 长到 600，跨视口顶（old_bottom=300 <= 500 < new_bottom=600）
case("case2 跨视口顶", win, area, container, cards, target=0, value_before=500, grow_to=600, expect=300)

# case 3: 卡2（y=600..900）完全在视口下方，value=500，长高
case("case3 全下方", win, area, container, cards, target=2, value_before=500, grow_to=500, expect=0)

sys.stdout.flush()
os._exit(0)
