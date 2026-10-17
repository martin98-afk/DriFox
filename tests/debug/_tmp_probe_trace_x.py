# -*- coding: utf-8 -*-
"""探针 Z：探针 X 场景中焦点在删除前后的演化追踪（用完即删）。"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_HERE = os.path.dirname(os.path.abspath(__file__))
exec(
    open(os.path.join(_HERE, "_tmp_probe_sds_recycle.py"), encoding="utf-8").read().split("# ─── 场景 1")[0]
)


def dump(tag):
    fw = QApplication.focusWidget()
    desc = type(fw).__name__ if fw else None
    where = "-"
    if fw is not None:
        try:
            if win.chat_scroll_area.isAncestorOf(fw):
                where = f"in_chat_area y={fw.mapTo(win.chat_container, fw.rect().topLeft()).y()}"
            else:
                where = "outside_chat"
        except RuntimeError:
            where = "deleted"
    p(f"    [{tag}] focus={desc} {where} value={sb.value()} max={sb.maximum()}")


win = build_window()
sb = win.chat_scroll_area.verticalScrollBar()
fw = focus_button_in_card(win, 0)
p(f"scrollbar={type(sb).__name__} 焦点=TransparentToolButton(卡0)")
dump("focus set")

sb.setValue(15 * (CARD_H + 6))
for _ in range(3):
    app.processEvents()
dump("mid scroll")

# 打桩 delete_widgets_from_layout 观察每张卡删除后的 value/焦点
import app.main_widget as mw  # noqa: E402

from app.widgets.ui_helpers import delete_widgets_from_layout as real_delete  # noqa: E402

_orig = real_delete
_calls = {"n": 0}

# monkeypatch 在 main_widget 模块里引用的名字
mw_delete = None
for name in dir(mw):
    if name == "delete_widgets_from_layout":
        mw_delete = getattr(mw, name)
        break
p(f"mw.delete_widgets_from_layout = {mw_delete}")


def _patched(widgets, layout, call_cleanup=True):
    _calls["n"] += 1
    p(f"  >>> delete#{_calls['n']}: {len(widgets)} widgets")
    dump("  before")
    r = _orig(widgets, layout, call_cleanup)
    dump("  after")
    return r


import app.widgets.ui_helpers as uh  # noqa: E402

uh.delete_widgets_from_layout = _patched
# main_widget 里是 from ... import delete_widgets_from_layout？查一下
import re  # noqa: E402


src = open("app/main_widget.py", encoding="utf-8").read()
m = re.search(r"from app\.widgets\.ui_helpers import[^\n]*", src)
p(f"import style: {m.group(0) if m else None}")

# 找 main_widget 中 delete_widgets_from_layout 的绑定
for name in ("delete_widgets_from_layout",):
    if hasattr(mw, name):
        setattr(mw, name, _patched)
        p("patched mw attr")

win._recycle_out_of_view_batches()
for _ in range(5):
    app.processEvents()
dump("after recycle")

v1 = sb.value()
p(f"drift 检查: value={v1}")
sys.stdout.flush()
os._exit(0)
