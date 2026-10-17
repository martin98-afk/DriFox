# -*- coding: utf-8 -*-
"""探针 J：单张卡删除，逐子步观测 valueChanged 触发点（用完即删）。

子步：cleanup() / removeWidget / setParent(None) / deleteLater / processEvents
"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_HERE = os.path.dirname(os.path.abspath(__file__))
exec(
    open(os.path.join(_HERE, "vscroll_recycle_rebuild_repro.py"), encoding="utf-8").read().split("def main()")[0]
)

win = build_window()
sb = win.chat_scroll_area.verticalScrollBar()
lay = win.chat_layout
container = win.chat_container

log = []
sb.valueChanged.connect(lambda v: log.append(("valueChanged", v)))

def flush(tag):
    if log:
        for e in log:
            print(f"    !! {tag}: {e}")
        log.clear()

sb.setValue(sb.maximum())
app.processEvents()
flush("warmup")
print(f"滚到底: value={sb.value()} max={sb.maximum()}")

card = win._batch_cards[0][0]
print(f"\n删除 card0（idx=0, h={card.height()}, viewer={card.viewer is not None}）")

print("step: cleanup()")
try:
    card.cleanup()
except Exception as e:
    print("  cleanup raised:", e)
flush("after cleanup")

print("step: layout.removeWidget")
lay.removeWidget(card)
flush("after removeWidget")
print(f"  value={sb.value()} max={sb.maximum()}")

print("step: hide()")
card.hide()
flush("after hide")

print("step: setParent(None)")
card.setParent(None)
flush("after setParent")
print(f"  value={sb.value()} max={sb.maximum()}")

print("step: deleteLater()")
card.deleteLater()
flush("after deleteLater")

print("step: processEvents x3")
for _ in range(3):
    app.processEvents()
flush("after processEvents")
print(f"  value={sb.value()} max={sb.maximum()}")
