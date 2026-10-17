# -*- coding: utf-8 -*-
"""临时探针 B：观察回收前后布局项构成与占位实际高度（用完即删）。"""

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
total = lambda: win.chat_container.sizeHint().height()  # noqa: E731


def dump(tag):
    print(f"--- {tag} ---")
    print(f"total(sizeHint)={total()}  container.height()={win.chat_container.height()}  max={sb.maximum()} value={sb.value()}")
    n = lay.count()
    print(f"layout items = {n}")
    ph_ids = {id(w): i for i, w in win._batch_placeholders.items()}
    for i in range(n):
        w = lay.itemAt(i).widget()
        if w is None:
            print(f"  [{i}] (no widget)")
            continue
        kind = "ph" if id(w) in ph_ids else ("card" if getattr(w, "_message_index", None) is not None else "?")
        idx = getattr(w, "_message_index", "-")
        print(f"  [{i}] {kind} msg_idx={idx} h={w.height()} fixed={w.minimumHeight()},{w.maximumHeight()} vis={w.isVisible()}")


# 装一个占位看它是否进布局并增高
sb.setValue(sb.maximum())
app.processEvents()
dump("初始（滚到底）")

# 手动装一个占位测试
ok = win._install_batch_placeholder(0, 300, 0, 0)
app.processEvents()
dump(f"手动装占位 batch0 -> {ok}")

print("PASS")
