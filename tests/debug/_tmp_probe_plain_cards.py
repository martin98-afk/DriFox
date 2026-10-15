# -*- coding: utf-8 -*-
"""探针 I：桩环境里用「普通 QWidget 卡」置换 MessageCard，看 value 跳变是否仍在（用完即删）。"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_HERE = os.path.dirname(os.path.abspath(__file__))
exec(
    open(os.path.join(_HERE, "vscroll_recycle_rebuild_repro.py"), encoding="utf-8").read().split("def main()")[0]
)

from PyQt5.QtWidgets import QWidget  # noqa: E402

win = build_window()
sb = win.chat_scroll_area.verticalScrollBar()
lay = win.chat_layout
container = win.chat_container

# 置换：删掉 MessageCard，装普通 QWidget 卡
while lay.count():
    item = lay.itemAt(0)
    w = item.widget() if item else None
    if w is None:
        lay.takeAt(0)
        continue
    lay.removeWidget(w)
    w.setParent(None)
    w.deleteLater()
for _ in range(3):
    app.processEvents()

plain_cards = []
for idx in range(30):
    w = QWidget(container)
    w.setFixedHeight(300)
    w._message_index = idx  # 桩属性
    lay.addWidget(w)
    plain_cards.append(w)
win._batch_cards = [[c] for c in plain_cards]
win._message_batch = [[{"role": "assistant", "content": f"b{idx}"}] for idx in range(30)]

for _ in range(3):
    app.processEvents()
container.adjustSize()
for _ in range(3):
    app.processEvents()

sb.setValue(sb.maximum())
for _ in range(2):
    app.processEvents()
print(f"滚到底: value={sb.value()} max={sb.maximum()} hint={container.sizeHint().height()}")

# 桩：_is_widget_alive 已 lambda True；_try_detach_card_viewer 已返回 False
win._recycle_out_of_view_batches()
for _ in range(3):
    app.processEvents()
print(f"回收后: value={sb.value()} max={sb.maximum()} hint={container.sizeHint().height()} c_h={container.height()}")
print(f"占位批次={sorted(win._batch_placeholders.keys())}")
