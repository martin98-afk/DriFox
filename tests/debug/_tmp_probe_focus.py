# -*- coding: utf-8 -*-
"""探针 L：hide() 触发 value 跳变时，焦点 widget 是谁（用完即删）。"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_HERE = os.path.dirname(os.path.abspath(__file__))
exec(
    open(os.path.join(_HERE, "vscroll_recycle_rebuild_repro.py"), encoding="utf-8").read().split("def main()")[0]
)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

win = build_window()
sb = win.chat_scroll_area.verticalScrollBar()
lay = win.chat_layout
area = win.chat_scroll_area


def dump_focus(tag):
    fw = QApplication.focusWidget()
    print(f"[{tag}] focusWidget={fw}")
    if fw is not None:
        try:
            print(f"    class={type(fw).__name__} y_in_container={fw.mapTo(win.chat_container, fw.rect().topLeft()).y()}")
        except Exception as e:
            print("    map failed:", e)
    print(f"    area.hasFocus={area.hasFocus()} focusPolicy={area.focusPolicy()}")


area.setFocus()
for _ in range(3):
    app.processEvents()
dump_focus("after setFocus on area")

sb.setValue(sb.maximum())
app.processEvents()
print(f"滚到底: value={sb.value()} max={sb.maximum()}")
dump_focus("at bottom")

card = win._batch_cards[0][0]
print(f"\ncard0: focusPolicy={card.focusPolicy()} hasFocus={card.hasFocus()}")
print(f"card0 focusProxy={card.focusProxy()}")
# 子控件里谁 focusPolicy != NoFocus
from PySide6.QtWidgets import QWidget  # noqa: E402

for child in card.findChildren(QWidget):
    if child.focusPolicy() != Qt.NoFocus:
        print(f"  child focusable: {type(child).__name__} policy={child.focusPolicy()} proxy={child.focusProxy()}")

lay.removeWidget(card)
print("\n=== hide() ===")
card.hide()
print(f"after hide: value={sb.value()} max={sb.maximum()}")
dump_focus("after hide")

print("\n=== 对照：先把焦点移给别人再 hide 另一张卡 ===")
sb.setValue(sb.maximum())
app.processEvents()
card3 = win._batch_cards[3][0]
lay.removeWidget(card3)
card3.hide()
print(f"after hide card3: value={sb.value()} max={sb.maximum()}")
dump_focus("after hide card3")
