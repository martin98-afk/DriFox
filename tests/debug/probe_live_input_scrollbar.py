# -*- coding: utf-8 -*-
"""在真实应用里探输入框状态：直接读运行中窗口的输入框，dump 判据链

用法：
  1. 先正常启动源码版应用（python main.py），输入超长内容复现无滚动条
  2. 另开终端跑：uv run python tests/debug/probe_live_input_scrollbar.py
  3. 脚本会 attach 到已有窗口（同进程内通过单实例端口不可行，改为
     直接在本进程内构建输入框做等价探测）

运行：uv run python tests/debug/probe_live_input_scrollbar.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from app.widgets.bottom_input_area import SendableTextEdit, _SCROLLBAR_MIN_MARGIN  # noqa: E402


def dump(box, label):
    vsb = box.verticalScrollBar()
    doc = box.document()
    vf = box.scrollDelegate.vScrollBar
    print(f"===== {label} =====")
    print("text repr:", repr(box.toPlainText()[:60]), "len:", len(box.toPlainText()))
    print("height:", box.height(), "min/max:", box.minimumHeight(), box.maximumHeight())
    print("viewport h:", box.viewport().height(), "doc h:", doc.size().height())
    print("vsb min/max/page/value:", vsb.minimum(), vsb.maximum(), vsb.pageStep(), vsb.value())
    print("余量 (max-pageStep):", vsb.maximum() - vsb.pageStep(), "阈值:", _SCROLLBAR_MIN_MARGIN)
    print("_needs_input_scrollbar:", box._needs_input_scrollbar())
    print("fluent visible:", vf.isVisible(), "forceHidden:", vf._isForceHidden)
    print("hbar policy (1=AlwaysOff):", box.verticalScrollBarPolicy())


# 场景 A：空白字符撑高（只换行/空格，无可见文字）
boxA = SendableTextEdit()
boxA.resize(600, 300)
boxA._initializing = False
boxA.show()
app.processEvents()
boxA.setPlainText("\n" * 40)
boxA._adjust_height_to_content()
app.processEvents()
dump(boxA, "A: 40 个换行（无可见文字）")

# 场景 B：可见长文本
boxB = SendableTextEdit()
boxB.resize(600, 300)
boxB._initializing = False
boxB.show()
app.processEvents()
boxB.setPlainText("\n".join(f"第 {i} 行测试文本内容" for i in range(30)))
boxB._adjust_height_to_content()
app.processEvents()
dump(boxB, "B: 30 行可见文本")

# 场景 C：极长单行（不换行，横向溢出）
boxC = SendableTextEdit()
boxC.resize(600, 300)
boxC._initializing = False
boxC.show()
app.processEvents()
boxC.setPlainText("很长的单行文本" * 200)
boxC._adjust_height_to_content()
app.processEvents()
dump(boxC, "C: 极长单行（不换行）")
