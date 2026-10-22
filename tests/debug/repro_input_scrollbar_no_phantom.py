# -*- coding: utf-8 -*-
"""反向验证：空态 / 单行输入不出现假滚轮（67efafc8 的原始诉求不能回归）

运行：uv run python tests/debug/repro_input_scrollbar_no_phantom.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from app.widgets.bottom_input_area import SendableTextEdit  # noqa: E402


def phantom(label, setup):
    box = SendableTextEdit()
    box.resize(600, 300)
    box._initializing = False
    box.show()
    app.processEvents()
    setup(box)
    app.processEvents()
    vf = box.scrollDelegate.vScrollBar
    print(f"[{label}] vsb.max={box.verticalScrollBar().maximum()} fluent_visible={vf.isVisible()}")
    # 原生 range 因 QSS padding 恒为 2，判据只看 Fluent 条是否可见
    assert not vf.isVisible(), f"{label}: 出现假滚轮"
    box.close()


phantom("空输入框", lambda b: None)
phantom("单行短文本", lambda b: b.setPlainText("hello"))
phantom("两三行文本", lambda b: b.setPlainText("第一行\n第二行\n第三行"))
print("通过：空/短输入均无假滚轮")
