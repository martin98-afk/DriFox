# -*- coding: utf-8 -*-
"""探针 AD：补偿判定矩阵 v2——类方法打补丁确保可观测（用完即删）。

组1 完全上方（期望补偿 +100）
组2 跨顶部·从40长高（prepend 动态加载场景，期望 +460 或 0，看设计语义）
组3 跨顶部·从300追加（流式语义，期望 0）
"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock  # noqa: E402

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication([])

import app.main_widget as mw  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

VIEWPORT_H = 400
N = 12
CALLS = []


def p(*a):
    print(*a)
    sys.stdout.flush()


_orig = mw.OpenAIChatToolWindow._on_message_card_height_changed


def _patched(self, h):
    try:
        snd = self.sender()
        sname = type(snd).__name__ if snd else None
        sdelta = getattr(snd, "_last_height_delta", None)
    except Exception as e:
        sname, sdelta = f"sender() raised {e!r}", None
    CALLS.append((sname, sdelta))
    p(f"      [patched] h={h} sender={sname} delta={sdelta}")
    return _orig(self, h)


mw.OpenAIChatToolWindow._on_message_card_height_changed = _patched


def build():
    from qfluentwidgets import SingleDirectionScrollArea

    win = mw.OpenAIChatToolWindow.__new__(mw.OpenAIChatToolWindow)
    for key, value in {
        "_is_destroyed": False,
        "_programmatic_scroll_depth": 0,
        "_scroll_max_cache": None,
        "_user_intentionally_away_from_bottom": True,
        "_bottom_anchor_deadline": 0.0,
        "_is_loading_history_batches": False,
        "_is_streaming": False,
        "_scroll_sync_timer": MagicMock(),
        "_layout_epoch": 0,
        "_node_user_cards_cache": None,
    }.items():
        setattr(win, key, value)

    area = SingleDirectionScrollArea()
    container = QWidget()
    lay = QVBoxLayout(container)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    area.setWidget(container)
    area.setWidgetResizable(True)
    area.resize(500, VIEWPORT_H)
    win.chat_scroll_area = area
    win.chat_container = container
    win.chat_layout = lay

    cards = []
    for i in range(N):
        card = MessageCard(role="assistant", parent=container)
        card.setFixedHeight(300)
        card._message_index = i
        card._content_just_loaded = False
        card.heightChanged.connect(win._on_message_card_height_changed)
        lay.addWidget(card)
        cards.append(card)
    area.show()
    container.show()
    for _ in range(4):
        app.processEvents()
    container.adjustSize()
    for _ in range(4):
        app.processEvents()
    return win, area, container, lay, cards


win, area, container, lay, cards = build()
sb = area.verticalScrollBar()
p(f"环境: max={sb.maximum()} value={sb.value()} 卡高 300×{N}")
p(f"补丁生效检查: {mw.OpenAIChatToolWindow._on_message_card_height_changed is _patched}")


def case(tag, *, target_idx, pre_value, grow_to, expect):
    CALLS.clear()
    sb.setValue(pre_value)
    for _ in range(3):
        app.processEvents()
    card = cards[target_idx]
    before_h = card.height()
    delta = grow_to - before_h
    card.setFixedHeight(grow_to)
    for _ in range(3):
        app.processEvents()
    v_before = sb.value()
    p(f'    receivers={cards[target_idx].receivers(cards[target_idx].heightChanged)}')
    card._last_height_delta = delta
    card2 = cards[target_idx]
    _hit = []
    card2.heightChanged.connect(lambda h: _hit.append(h))
    p(f'    emit 前 receivers={card2.receivers(card2.heightChanged)}')
    card2.heightChanged.emit(grow_to)
    p(f'    emit 后 立即 CALLS={CALLS}  lam_hit={_hit}')
    for _ in range(2):
        app.processEvents()
    actual = sb.value() - v_before
    mark = "✅" if actual == expect else f"❌ 期望 {expect:+d}"
    p(f"[{tag}] value={pre_value} 卡{target_idx} {before_h}->{grow_to} (delta={delta:+d})  实测={actual:+d}  {mark}")
    p(f"    CALLS={CALLS}")


case("组1 完全上方", target_idx=0, pre_value=1000, grow_to=400, expect=100)
p("")
cards[0].setFixedHeight(40)
for _ in range(3):
    app.processEvents()
case("组2 跨顶部·从40长高", target_idx=0, pre_value=320, grow_to=500, expect=460)
p("")
cards[0].setFixedHeight(300)
for _ in range(3):
    app.processEvents()
case("组3 跨顶部·从300追加", target_idx=0, pre_value=200, grow_to=600, expect=0)

sys.stdout.flush()
os._exit(0)
