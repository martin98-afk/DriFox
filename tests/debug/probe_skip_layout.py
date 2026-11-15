# -*- coding: utf-8 -*-
"""探针 v7：验证「跳过全量布局」后输入框 textChanged 剩余开销（offscreen）

v4 结论：_on_text_changed 里 document().size() 一次吃掉全部时间（500k=4556ms）。
本探针回答修复可行性：
  1. 把 doc.size() 打桩成常量后，_on_text_changed 全链还剩多少
  2. _on_fluent_vbar_range_changed 里读 verticalScrollBar().maximum() 是否也会
     逼出全量布局（如果会，滚条收放也得改成无布局判据）
  3. QTextDocument.blockCount() 是否 O(1)（可做超长判据）

运行：
  $env:QT_QPA_PLATFORM="offscreen"; python -u tests\debug\probe_skip_layout.py
"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DRIFOX_NO_CODEBUDDY_REFRESH", "1")
os.environ.setdefault("DRIFOX_DATA_DIR", "D:\\work\\DriFox\\.drifox-test")

from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from PyQt5.QtGui import QTextDocument  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from app.widgets.bottom_input_area import SendableTextEdit  # noqa: E402


def make_text(n_chars: int, line_len: int = 80) -> str:
    unit = "这是一段用于测试发送卡顿的超长文本内容 abcdefghij"
    lines, total = [], 0
    while total < n_chars:
        lines.append((unit * (line_len // len(unit) + 2))[:line_len])
        total += line_len + 1
    return "\n".join(lines)


def ms(fn):
    t0 = time.perf_counter()
    r = fn()
    return r, (time.perf_counter() - t0) * 1000


def probe(n_chars: int):
    print(f"\n===== {n_chars} 字符 =====", flush=True)
    text = make_text(n_chars)

    box = SendableTextEdit()
    box.resize(900, 120)
    box._initializing = False
    box.show()
    app.processEvents()

    # 基线：完整链路
    _, t_full = ms(lambda: box.setPlainText(text))
    print(f"  完整 setPlainText              {t_full:9.2f} ms", flush=True)

    # 文档已布局完毕，此时测 blockCount 与 vsb.maximum 的边际成本
    _, t_bc = ms(lambda: box.document().blockCount())
    print(f"  document().blockCount()(已布局) {t_bc:9.2f} ms  = {box.document().blockCount()}", flush=True)
    _, t_vsb = ms(lambda: box.verticalScrollBar().maximum())
    print(f"  verticalScrollBar().maximum()  {t_vsb:9.2f} ms  = {box.verticalScrollBar().maximum()}", flush=True)

    # 打桩 doc.size()：验证「不问布局」时 textChanged 剩余开销
    orig_size = QTextDocument.size

    def fake_size(self):
        from PyQt5.QtCore import QSizeF
        return QSizeF(self.textWidth(), 1000.0)

    QTextDocument.size = fake_size
    try:
        box2 = SendableTextEdit()
        box2.resize(900, 120)
        box2._initializing = False
        box2.show()
        app.processEvents()

        # 先清空布局缓存：setPlainText 新文本
        _, t_nolayout = ms(lambda: box2.setPlainText(text))
        print(f"  setPlainText(打桩 size 后)     {t_nolayout:9.2f} ms", flush=True)

        # 单独测滚条余量（未触发全量布局时是否昂贵）
        _, t_vsb2 = ms(lambda: box2.verticalScrollBar().maximum())
        print(f"  vsb.maximum()(未全量布局)      {t_vsb2:9.2f} ms  = {box2.verticalScrollBar().maximum()}", flush=True)
        _, t_bc2 = ms(lambda: box2.document().blockCount())
        print(f"  blockCount()(未全量布局)       {t_bc2:9.2f} ms", flush=True)
        box2.close()
    finally:
        QTextDocument.size = orig_size
    box.close()


if __name__ == "__main__":
    for n in [int(x) for x in (sys.argv[1:] or ["100000", "500000"])]:
        probe(n)
