# -*- coding: utf-8 -*-
"""临时探针 9：复现「原位重建 ↔ 回收」无限往复（用完即删）

对齐用户实机日志形态（01:56:20~01:56:39，连续 20+ 轮无操作）：
    每 0.5s 一轮，每轮「原位重建 2 个」+「回收 2 个离屏批次」
    batches=50/9 ↔ 50/11 往复
"""

import os
import sys

os.environ["DRIFOX_NO_CODEBUDDY_REFRESH"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_HERE = os.path.dirname(os.path.abspath(__file__))
exec(
    open(os.path.join(_HERE, "vscroll_recycle_rebuild_repro.py"), encoding="utf-8").read().split("def main()")[0]
)

# 对齐实机规模（batches=50）：把 BATCH 提到 50 后重建窗口。
# build_window 内部用模块级 BATCH 常量，这里直接改它再调用。
BATCH = 50
build_window.__globals__["BATCH"] = 50

BATCH_COUNT = 50

win = build_window()
sb = win.chat_scroll_area.verticalScrollBar()
qapp = app
layout = win.chat_layout
total = lambda: win.chat_container.sizeHint().height()  # noqa: E731
nonempty = lambda: sum(1 for b in win._batch_cards if b)  # noqa: E731


def snap():
    return {
        "value": sb.value(),
        "max": sb.maximum(),
        "total": total(),
        "nonempty": nonempty(),
        "ph": set(win._batch_placeholders.keys()),
    }


# 视口滚到中段（对齐实机：用户滚到中段停下）
sb.setValue(20 * CARD_H)
app.processEvents()
print(f"[init] vp={win._viewport_batch_range()} value={sb.value()} total={total()} nonempty={nonempty()}")

# 先跑一轮让上下装占位
win._recycle_out_of_view_batches()
app.processEvents()
print(f"[warm] vp={win._viewport_batch_range()} value={sb.value()} total={total()} nonempty={nonempty()}")
print()

print("=== 连续 6 轮（视口不动）===")
for round_no in range(1, 7):
    before = snap()
    vp = win._viewport_batch_range()
    win._recycle_out_of_view_batches()
    app.processEvents()
    app.processEvents()
    after = snap()

    rebuilt = [i for i in range(len(win._batch_cards)) if i not in before["ph"] and i in after["ph"]]
    # 重建 = 轮前是占位(或 None)、轮后非空
    rebuilt = [i for i in range(len(win._batch_cards)) if win._batch_cards[i] and i in before["ph"]]
    recycled = [i for i, b in enumerate(win._batch_cards) if b is None and i not in before["ph"]]

    print(
        f"round {round_no}: vp={vp} -> {win._viewport_batch_range()}  "
        f"value {before['value']}->{after['value']}  "
        f"total {before['total']}->{after['total']}  "
        f"nonempty {before['nonempty']}->{after['nonempty']}"
    )
    print(f"          重建={rebuilt}  回收={recycled}")
