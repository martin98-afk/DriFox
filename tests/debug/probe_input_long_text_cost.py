# -*- coding: utf-8 -*-
"""探针 v2：输入框 setPlainText / 粘贴路径超线性增长的成分拆解（offscreen）

v1 实测（sizes 1000/10000/100000/500000）：
  setPlainText  0.57 / 4.52 / 187.40 / 4333.20 ms   ← 超线性
  高亮 rehighlight 0.52 / 0.56 / 2.48 / 10.94 ms     ← 线性，不是主因
  PlainTextViewer.set_text 1.55 / 1.51 / 3.83 / 8.61 ms ← 已有 O(1) 快速路径

本探针回答：setPlainText 的 4333ms 花在哪
  1. 基线（裸 QTextEdit.setPlainText，无子类重写/无信号）
  2. 断开 textChanged 的 SendableTextEdit
  3. 断开高亮器
  4. 完整链路
  5. 真实粘贴路径 insertFromMimeData

运行：
  $env:QT_QPA_PLATFORM="offscreen"; python -u tests\debug\probe_input_long_text_cost.py
"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DRIFOX_NO_CODEBUDDY_REFRESH", "1")

from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from PyQt5.QtCore import QMimeData  # noqa: E402
from PyQt5.QtWidgets import QApplication, QTextEdit  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from app.widgets.bottom_input_area import SendableTextEdit  # noqa: E402


def make_text(n_chars: int, line_len: int = 80) -> str:
    unit = "这是一段用于测试发送卡顿的超长文本内容 abcdefghij"
    lines = []
    total = 0
    while total < n_chars:
        lines.append((unit * (line_len // len(unit) + 2))[:line_len])
        total += line_len + 1
    return "\n".join(lines)


def ms(fn):
    t0 = time.perf_counter()
    fn()
    return (time.perf_counter() - t0) * 1000


def probe(n_chars: int):
    print(f"\n===== {n_chars} 字符 =====", flush=True)
    text = make_text(n_chars)

    # 1) 裸 QTextEdit（无重写、无信号、无高亮）
    raw = QTextEdit()
    raw.resize(900, 120)
    raw.show()
    app.processEvents()
    print(f"  裸 QTextEdit.setPlainText          {ms(lambda: raw.setPlainText(text)):9.2f} ms", flush=True)

    # 2) 完整 SendableTextEdit（含高亮 + 全部 textChanged 槽）
    box = SendableTextEdit()
    box.resize(900, 120)
    box._initializing = False
    box.show()
    app.processEvents()
    print(f"  SendableTextEdit.setPlainText      {ms(lambda: box.setPlainText(text)):9.2f} ms", flush=True)

    # 3) 同实例：阻塞信号再 setPlainText
    box.blockSignals(True)
    print(f"  + blockSignals(True)               {ms(lambda: box.setPlainText(text)):9.2f} ms", flush=True)
    box.blockSignals(False)
    app.processEvents()

    # 4) 断开高亮器（保留 textChanged 槽）
    hl = box._placeholder_highlighter
    hl.setDocument(None)
    print(f"  + 摘掉高亮器                        {ms(lambda: box.setPlainText(text)):9.2f} ms", flush=True)

    # 5) 摘掉高亮器后再 blockSignals
    box.blockSignals(True)
    print(f"  + 摘高亮器 + blockSignals           {ms(lambda: box.setPlainText(text)):9.2f} ms", flush=True)
    box.blockSignals(False)
    app.processEvents()

    # 6) 真实粘贴路径 insertFromMimeData（纯文本）
    box2 = SendableTextEdit()
    box2.resize(900, 120)
    box2._initializing = False
    box2.show()
    app.processEvents()
    mime = QMimeData()
    mime.setText(text)
    print(f"  insertFromMimeData(粘贴路径)        {ms(lambda: box2.insertFromMimeData(mime)):9.2f} ms", flush=True)

    # 7) 粘贴后 clear()
    print(f"  粘贴后 clear()                      {ms(box2.clear):9.2f} ms", flush=True)

    for w in (raw, box, box2):
        w.close()


if __name__ == "__main__":
    sizes = [int(x) for x in (sys.argv[1:] or ["100000", "500000"])]
    for n in sizes:
        probe(n)
