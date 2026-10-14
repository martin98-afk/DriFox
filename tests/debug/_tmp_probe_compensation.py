# -*- coding: utf-8 -*-
"""探针 AC：`_on_message_card_height_changed` 补偿分支的几何判定矩阵（用完即删）。

三组场景（非跟随态，用户在中段阅读）：
  1. 卡完全在视口上方（old/new bottom < value）→ 期望补偿 delta
  2. 卡跨视口顶部、从 40px 懒渲染长到 500px（old_bottom < value < new_bottom）
     → prepend 动态加载场景，期望补偿 delta（保持视口顶对齐内容不变）
  3. 卡跨视口顶部、从已有高度追加（old_bottom > value）→ 期望不补偿（P052 流式语义）
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

from app.main_widget import OpenAIChatToolWindow  # noqa: E402
from app.widgets.message_card import MessageCard  # noqa: E402

VIEWPORT_H = 400
N = 12


def p(*a):
    print(*a)
    sys.stdout.flush()


class Harness(OpenAIChatToolWindow):
    """带真实方法的最小载体（不跑 __init__）。"""


def build():
    """真 SingleDirectionScrollArea + 真实方法（_on_message_card_height_changed 走真实实现）。"""
    from qfluentwidgets import SingleDirectionScrollArea

    win = Harness.__new__(Harness)
    for key, value in {
        "_is_destroyed": False,
        "_programmatic_scroll_depth": 0,
        "_scroll_max_cache": None,
        "_user_intentionally_away_from_bottom": True,  # 非跟随态：用户在中段阅读
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
        card.heightChanged.connect(win._on_message_card_height_changed)  # 真实入口连接
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


def case(tag, win, area, container, cards, *, target_idx, pre_value, grow_to, expect_delta):
    """设置视口 value → 对 target 卡长高 → 读 value 变化。"""
    sb = area.verticalScrollBar()
    sb.setValue(pre_value)
    for _ in range(3):
        app.processEvents()

    card = cards[target_idx]
    before_h = card.height()
    before_bottom = card.mapTo(container, card.rect().topLeft()).y() + before_h
    delta = grow_to - before_h

    card.setFixedHeight(grow_to)
    for _ in range(3):
        app.processEvents()
    after_bottom = card.mapTo(container, card.rect().topLeft()).y() + card.height()

    v_before = sb.value()
    card._last_height_delta = delta
    # spy：包装真实方法后重新连接
    _orig = type(win)._on_message_card_height_changed

    def _spy(self, h):
        snd = self.sender()
        p(f'      [spy] enter sender={type(snd).__name__ if snd else None} delta={getattr(snd, "_last_height_delta", None)}')
        r = _orig(self, h)
        p(f'      [spy] exit value={sb.value()} follow={self._should_follow_bottom()}')
        return r

    try:
        card.heightChanged.disconnect()
    except Exception:
        pass
    card.heightChanged.connect(_spy.__get__(win, type(win)))
    card.heightChanged.emit(grow_to)
    for _ in range(2):
        app.processEvents()
    v_after = sb.value()
    actual = v_after - v_before

    verdict = "✅" if actual == expect_delta else f"❌（期望 {expect_delta:+d}）"
    p(f"[{tag}]")
    p(f"    value={pre_value} 卡{target_idx} h {before_h}->{grow_to}  bottom {before_bottom}->{after_bottom}")
    p(f"    delta={delta:+d}  补偿实际={actual:+d}  {verdict}")

    # 恢复
    card.setFixedHeight(300)
    for _ in range(3):
        app.processEvents()


win, area, container, lay, cards = build()
sb = area.verticalScrollBar()
p(f"环境: max={sb.maximum()} 卡高 300×{N}")

# 组 1：卡完全在视口上方（卡0: y=0..300），value=1000
case("组1 完全上方", win, area, container, cards, target_idx=0, pre_value=1000, grow_to=400, expect_delta=100)

# 组 2：卡跨越视口顶部，从 40 长到 500（prepend 动态加载场景）
#     先把卡1 压到 40 高，value 定在卡1 内部（y=0..40 时，value=320 在卡1 下方…）
#      直接构造：卡0 压成 40，视口 value=320 → 卡0 y=0..40（old_bottom=40 < 320）
p("")
cards[0].setFixedHeight(40)
for _ in range(3):
    app.processEvents()
case("组2 跨顶部·从40长高", win, area, container, cards, target_idx=0, pre_value=320, grow_to=500, expect_delta=460)

# 组 3：卡跨越视口顶部、从已有高度追加（流式语义）
#      卡0 高 300（y=0..300），value=200（视口顶在卡0 内部），长到 600
p("")
case("组3 跨顶部·从300追加", win, area, container, cards, target_idx=0, pre_value=200, grow_to=600, expect_delta=0)

sys.stdout.flush()
os._exit(0)
