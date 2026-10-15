# -*- coding: utf-8 -*-
"""临时探针 D：抓 setValue 调用来源 + 布局 hint 演化（用完即删）。"""

import os
import sys
import traceback

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_HERE = os.path.dirname(os.path.abspath(__file__))
exec(
    open(os.path.join(_HERE, "vscroll_recycle_rebuild_repro.py"), encoding="utf-8").read().split("def main()")[0]
)

import app.main_widget as mw  # noqa: E402
from PyQt5.QtWidgets import QScrollBar  # noqa: E402

win = build_window()
sb = win.chat_scroll_area.verticalScrollBar()
lay = win.chat_layout
container = win.chat_container

_hint = lambda: container.sizeHint().height()  # noqa: E731

_orig_setvalue = sb.setValue
_orig_setmax = sb.setMaximum
_orig_setrange = sb.setRange


def _fmt_frame(fr):
    return f"{fr.filename.split(chr(92))[-1]}:{fr.lineno} in {fr.name}"


def _log_setvalue(v, tag):
    stack = traceback.extract_stack()
    # 取从 app/ 或 tests/ 起的最近 4 帧
    frames = [_fmt_frame(f) for f in stack[:-1]]
    interesting = [f for f in frames if ("app" in f or "tests" in f)]
    print(f"    [{tag}] setValue({v})  prev={sb.value()} max={sb.maximum()} hint={_hint()} c_h={container.height()}")
    for f in interesting[-6:]:
        print(f"        <- {f}")


def _sv(v):
    _log_setvalue(v, "sv")
    _orig_setvalue(v)


def _sm(v):
    _log_setvalue(v, "setMaximum")
    _orig_setmax(v)


sb.setValue = _sv
sb.setMaximum = _sm

sb.setValue(sb.maximum())
app.processEvents()
print(f"滚到底 value={sb.value()} max={sb.maximum()} hint={_hint()}")

print("\n=== 开始回收 ===")
win._recycle_out_of_view_batches()
app.processEvents()
print(f"\n回收后 value={sb.value()} max={sb.maximum()} hint={_hint()} c_h={container.height()}")
print(f"占位批次={sorted(win._batch_placeholders.keys())}")
alive = [b for b in win._batch_cards if b]
print(f"存活批次数={len(alive)}")
