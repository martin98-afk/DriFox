# -*- coding: utf-8 -*-
"""关键场景：高度没推到上限、但内容已超长 → 滚动条必须仍然显示

这条正是用户截图（内容超长却无滚动条）的复现路径：
`_adjust_height_to_content` 因各种原因没把 height 推到 300
（初始化标志未清 / 拖拽期跳过 / 文档高度未算出），
旧判据（height >= maximumHeight）会误判为不需要滚动。

运行：uv run python tests/debug/repro_input_scrollbar_stuck_height.py
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

long_text = "\n".join(f"第 {i} 行内容测试文本" for i in range(30))
box.setPlainText(long_text)
app.processEvents()

# 关键：强制把高度压回 46（模拟 _adjust_height_to_content 未生效的异常路径），
# 但文档内容仍是超长的 → 原生 range 远大于 pageStep
box.setFixedHeight(46)
box.resize(600, 46)
app.processEvents()
print("height:", box.height(), "（被强制压在低位）")
print("vsb max:", box.verticalScrollBar().maximum())
print("vsb pageStep:", box.verticalScrollBar().pageStep())
print("needs_scrollbar:", box._needs_input_scrollbar())

box._on_fluent_vbar_range_changed()
vf = box.scrollDelegate.vScrollBar
print("fluent visible:", vf.isVisible())

assert box._needs_input_scrollbar(), "内容超长时判据必须为真（不依赖高度是否到上限）"
assert vf.isVisible(), "内容超长但高度未推上去时，滚动条仍应显示"
print("通过：高度未推上去时滚动条依然显示")
