# -*- coding: utf-8 -*-
"""临时探针 C：逐张删除卡片时同步观测 value/max/container 高度（用完即删）。"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_HERE = os.path.dirname(os.path.abspath(__file__))
exec(
    open(os.path.join(_HERE, "vscroll_recycle_rebuild_repro.py"), encoding="utf-8").read().split("def main()")[0]
)

import app.main_widget as mw  # noqa: E402

win = build_window()
sb = win.chat_scroll_area.verticalScrollBar()
lay = win.chat_layout
container = win.chat_container

_orig_delete = mw.delete_widgets_from_layout
_calls = {"n": 0}


def snap():
    return (
        f"v={sb.value()} max={sb.maximum()} c_h={container.height()} "
        f"hint={container.sizeHint().height()} live={lay.count()}"
    )


def _patched(widgets, layout, call_cleanup=True):
    _calls["n"] += 1
    n = _calls["n"]
    print(f"\n=== delete#{n} begin: {len(widgets)} widgets ===")
    print(f"  before: {snap()}")
    for w in list(widgets):
        before = snap()
        _orig_delete([w], layout, call_cleanup)
        w.setAttribute(111, True)  # WA_DeleteOnClose? no-op 防优化
        after = snap()
        print(f"  one card: {before}  ->  {after}")
    print(f"  end: {snap()}")


mw.delete_widgets_from_layout = _patched

sb.setValue(sb.maximum())
app.processEvents()
print(f"滚到底: {snap()}")

win._recycle_out_of_view_batches()
app.processEvents()
print(f"\n回收后: {snap()}")
