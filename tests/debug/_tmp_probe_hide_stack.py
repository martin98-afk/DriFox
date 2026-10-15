# -*- coding: utf-8 -*-
"""探针 K：hide() 触发 valueChanged 的调用栈（用完即删）。"""

import os
import sys
import traceback

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_HERE = os.path.dirname(os.path.abspath(__file__))
exec(
    open(os.path.join(_HERE, "vscroll_recycle_rebuild_repro.py"), encoding="utf-8").read().split("def main()")[0]
)

win = build_window()
sb = win.chat_scroll_area.verticalScrollBar()
lay = win.chat_layout

_captured = []


def _on_value_changed(v):
    stack = traceback.extract_stack()
    _captured.append((v, stack))
    print(f"!!! valueChanged({v}) stack:")
    for fr in stack[:-1]:
        fn = fr.filename.replace("\\", "/")
        if "/app/" in fn or "/tests/" in fn or "qfluentwidgets" in fn or "/PyQt5/" in fn:
            print(f"    {fn.split('/')[-1]}:{fr.lineno} in {fr.name}")


sb.valueChanged.connect(_on_value_changed)

sb.setValue(sb.maximum())
app.processEvents()
print(f"滚到底: value={sb.value()} max={sb.maximum()}")

card = win._batch_cards[0][0]
lay.removeWidget(card)
print("\n=== hide() ===")
card.hide()
print(f"\nvalue now={sb.value()} max={sb.maximum()}")
