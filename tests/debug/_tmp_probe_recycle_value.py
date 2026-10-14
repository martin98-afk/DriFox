# -*- coding: utf-8 -*-
"""临时探针 A：回收过程中 value/maximum/total 的逐步变化（用完即删）。

目的：vscroll_recycle_rebuild_repro 里「回收后 value 8602→5899」 unexplained，
      查明是哪一步、由哪个信号/setValue 引起的。
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
total = lambda: win.chat_container.sizeHint().height()  # noqa: E731

_events = []


def _bind(tag, sig):
    def _cb(*args):
        _events.append((tag, args, sb.value(), sb.maximum(), total()))

    sig.connect(_cb)


_bind("value", sb.valueChanged)
_bind("range", sb.rangeChanged)

# 打桩关键步骤
_orig_install = win._install_batch_placeholder
_orig_delete = win.chat_layout.removeWidget  # not used; keep


def _mark(tag):
    _events.append((tag, (), sb.value(), sb.maximum(), total()))


import app.main_widget as mw  # noqa: E402

_orig_delete_widgets = mw.delete_widgets_from_layout


def _patched_delete(widgets, layout, call_cleanup=True):
    _mark("delete_begin")
    r = _orig_delete_widgets(widgets, layout, call_cleanup)
    _mark("delete_end")
    return r


mw.delete_widgets_from_layout = _patched_delete

sb.setValue(sb.maximum())
app.processEvents()
_mark("at_bottom")
print(f"滚到底 value/max = {sb.value()}/{sb.maximum()} total = {total()}")

_events.clear()
win._recycle_out_of_view_batches()
app.processEvents()
_mark("after_recycle")

print(f"回收后 value/max = {sb.value()}/{sb.maximum()} total = {total()}")
print()
print("=== 事件序列 ===")
for e in _events:
    print(e)
