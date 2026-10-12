# -*- coding: utf-8 -*-
"""置底贴底露白探针：证实「预抬滚动条上界 + 贴上 → 布局滞后一拍 → 底部露白」机制。

复刻 main_widget._on_message_card_height_changed 跟随态分支的代码序列：
    sb.setMaximum(sb.maximum() + delta)
    sb.setValue(sb.maximum())
在卡片 setFixedHeight 之后、Qt 布局落地之前执行。每一步打印
value + viewport vs container.height() 的差值（>0 即露白 px 数）。

用法：uv run python tests/debug/probe_bottom_stick_flash.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import (
    QApplication,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from PyQt5.QtCore import Qt

app = QApplication(sys.argv)

scroll = QScrollArea()
scroll.setWidgetResizable(True)
container = QWidget()
lay = QVBoxLayout(container)
lay.setContentsMargins(6, 6, 6, 6)
lay.setSpacing(6)
lay.setAlignment(Qt.AlignBottom)

cards = []
for i in range(3):
    w = QWidget()
    w.setFixedHeight(200)
    lay.addWidget(w)
    cards.append(w)

scroll.setWidget(container)
scroll.resize(400, 600)
scroll.show()
app.processEvents()

sb = scroll.verticalScrollBar()
vp_h = scroll.viewport().height()


def report(tag: str) -> None:
    landed_max = container.height() - vp_h
    flash = sb.value() + vp_h - container.height()
    print(
        f"[{tag}] value={sb.value()} sb_max={sb.maximum()} "
        f"container.h={container.height()} landed_max={landed_max} "
        f"露白={'%.0fpx' % flash if flash > 0 else '无'}"
    )


# 初始贴底（布局已落地）
sb.setValue(sb.maximum())
app.processEvents()
report("初始贴底")

# ── 模拟流式 tick：3 拍，每拍内容增高 40px ──
# 复刻 main_widget 跟随态分支的顺序：setFixedHeight（布局请求 post，未落地）
# → setMaximum(预抬) → setValue(贴上)
for tick in range(1, 4):
    card = cards[-1]
    card.setFixedHeight(card.height() + 40)
    delta = 40
    sb.setMaximum(max(0, sb.maximum() + delta))
    with_pre = sb.maximum()
    sb.setValue(sb.maximum())
    report(f"tick{tick} 贴上预抬上界(max={with_pre})")
    # 布局落地（等价于事件循环跑完 LayoutRequest / QScrollArea 自适应）
    app.processEvents()
    report(f"tick{tick} 布局落地后")

# ── 对照组：贴「已落地」底（container.height() 换算）──
card = cards[-1]
card.setFixedHeight(card.height() + 40)
app.processEvents()
landed = max(0, min(sb.maximum(), container.height() - vp_h))
sb.setValue(landed)
report("对照: 贴已落地底")

flash_any = False
print("done")
