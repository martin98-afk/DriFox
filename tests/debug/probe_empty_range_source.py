# -*- coding: utf-8 -*-
"""探查：空输入框下原生竖条 range 为何是 2（决定假滚轮判据）

运行：uv run python tests/debug/probe_empty_range_source.py
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

vsb = box.verticalScrollBar()
print("native vsb: min", vsb.minimum(), "max", vsb.maximum(), "value", vsb.value())
print("viewport h:", box.viewport().height(), "doc h:", box.document().size().height())
print("box h:", box.height(), "min:", box.minimumHeight(), "max:", box.maximumHeight())

# 量一下 Fluent 条的 partnerBar 是不是同一个对象
vf = box.scrollDelegate.vScrollBar
print("fluent partnerBar is native vsb:", vf.partnerBar is vsb)
print("fluent bar max:", vf.maximum(), "visible:", vf.isVisible())
print("hbar policy:", box.verticalScrollBarPolicy())
