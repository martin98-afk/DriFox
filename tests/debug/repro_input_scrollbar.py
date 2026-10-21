# -*- coding: utf-8 -*-
"""正向验证：超长内容出现滚动条，删短后收条；且滚轮事件能真正滚动

运行：uv run python tests/debug/repro_input_scrollbar.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from app.widgets.bottom_input_area import SendableTextEdit  # noqa: E402

box = SendableTextEdit()
box.resize(600, 300)
box._initializing = False
box.show()
app.processEvents()

vf = box.scrollDelegate.vScrollBar
vsb = box.verticalScrollBar()


def state(label):
    app.processEvents()
    print(
        f"[{label}] h={box.height()} doc={box.document().size().height():.0f} "
        f"viewport={box.viewport().height()} vsb_max={vsb.maximum()} "
        f"fluent_visible={vf.isVisible()} vsb_value={vsb.value()}"
    )


long_text = "\n".join(f"第 {i} 行内容测试文本" for i in range(30))
box.setPlainText(long_text)
box._adjust_height_to_content()
state("超长内容")

assert box.height() == 300, "应被 300 上限压住"
assert vf.isVisible(), "超长内容应显示 Fluent 滚动条"
assert vsb.maximum() > 2, "内容应真的可滚"

# 滚轮向下：原生 scrollbar 值应增加
before = vsb.value()
from PyQt5.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QWheelEvent  # noqa: E402

ev = QWheelEvent(
    QPointF(100, 100),
    box.mapToGlobal(QPoint(100, 100)),
    QPoint(0, 0),
    QPoint(0, -120),
    0,
    Qt.Vertical,
    Qt.NoButton,
    Qt.NoModifier,
    Qt.ScrollPhase.NoScrollPhase,
    False,
)
QApplication.sendEvent(box.viewport(), ev)
for _ in range(30):
    app.processEvents()
    import time as _t

    _t.sleep(0.02)
    if vsb.value() > before:
        break
state("滚轮向下")
print("滚轮使 vsb.value:", before, "->", vsb.value())
assert vsb.value() > before, "滚轮应使输入框向下滚动"

# 删短内容：应收条
box.setPlainText("短内容")
box._adjust_height_to_content()
state("删短内容")
assert box.height() < 300, "短内容应回落到 300 以下"
assert not vf.isVisible(), "删短后应收起滚动条"

print("通过：超长出条、可滚、删短收条")
