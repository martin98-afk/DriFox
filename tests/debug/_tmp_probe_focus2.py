# -*- coding: utf-8 -*-
"""探针 L2：极简 hide() 复现 + 焦点差量（不用 findChildren，用完即删）。"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_HERE = os.path.dirname(os.path.abspath(__file__))
exec(
    open(os.path.join(_HERE, "vscroll_recycle_rebuild_repro.py"), encoding="utf-8").read().split("def main()")[0]
)

from PySide6.QtWidgets import QApplication  # noqa: E402


def p(*a):
    print(*a)
    sys.stdout.flush()


win = build_window()
sb = win.chat_scroll_area.verticalScrollBar()
lay = win.chat_layout
area = win.chat_scroll_area

p("focusWidget initial:", QApplication.focusWidget())
sb.setValue(sb.maximum())
app.processEvents()
p(f"滚到底: value={sb.value()} max={sb.maximum()}")
p("focusWidget at bottom:", QApplication.focusWidget())

card = win._batch_cards[0][0]
p(f"card0 focusPolicy={card.focusPolicy()} focusProxy={card.focusProxy()}")

lay.removeWidget(card)
p("=== calling hide() ===")
card.hide()
p(f"after hide: value={sb.value()} max={sb.maximum()}")
p("focusWidget after hide:", QApplication.focusWidget())

# 对照：不涉及焦点的另一张（value 已低）
sb.setValue(sb.maximum())
app.processEvents()
p(f"\nreset: value={sb.value()}")
card3 = win._batch_cards[3][0]
lay.removeWidget(card3)
card3.hide()
p(f"hide card3: value={sb.value()} max={sb.maximum()}")
