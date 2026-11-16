# -*- coding: utf-8 -*-
"""探针 v4：_on_text_changed 内部逐段拆解（offscreen）

v3 结论：setPlainText 只触发 1 次 textChanged，全部耗时在 _on_text_changed
（10万 186.7ms / 50万 4535.8ms，超线性 ≈n^2）。

_on_text_changed 内部步骤：
  a. self.toPlainText()            全量快照
  b. text.strip()
  c. send_btn.set_send_enabled()
  d. _adjust_height_to_content()   doc.size().height() → 触发文档布局
  e. _sync_placeholder_removals()  正则 findall
  f. _schedule_detail_sync()       timer start

运行：
  $env:QT_QPA_PLATFORM="offscreen"; python -u tests\debug\probe_on_text_changed_parts.py
"""

import os
import re
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DRIFOX_NO_CODEBUDDY_REFRESH", "1")

from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from PyQt5.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from app.widgets.bottom_input_area import SendableTextEdit, _PLACEHOLDER_RE  # noqa: E402


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
    box = SendableTextEdit()
    box.resize(900, 120)
    box._initializing = False
    box.show()
    app.processEvents()

    text = make_text(n_chars)
    box.blockSignals(True)
    box.setPlainText(text)
    box.blockSignals(False)
    app.processEvents()

    _, t_toplain = ms(box.toPlainText)
    _, t_strip = ms(lambda: text.strip())
    _, t_findall = ms(lambda: _PLACEHOLDER_RE.findall(text))
    _, t_docsize = ms(lambda: box.document().size())
    box._adjusting_height = False
    _, t_adjust = ms(box._adjust_height_to_content)
    _, t_syncph = ms(lambda: box._sync_placeholder_removals(text))

    print(f"  toPlainText()               {t_toplain:9.2f} ms", flush=True)
    print(f"  text.strip()                {t_strip:9.2f} ms", flush=True)
    print(f"  _PLACEHOLDER_RE.findall     {t_findall:9.2f} ms", flush=True)
    print(f"  document().size()           {t_docsize:9.2f} ms", flush=True)
    print(f"  _adjust_height_to_content   {t_adjust:9.2f} ms", flush=True)
    print(f"  _sync_placeholder_removals  {t_syncph:9.2f} ms", flush=True)

    # 关键对照：文档布局是否已被首次 size() 缓存
    _, t_docsize2 = ms(lambda: box.document().size())
    print(f"  document().size() 二次      {t_docsize2:9.2f} ms", flush=True)

    # 文档几何：行数、文本宽
    doc = box.document()
    print(
        f"  文档 block 数={doc.blockCount()}  textWidth={doc.textWidth():.0f} "
        f"docMargin={doc.documentMargin()} viewport宽={box.viewport().width()}",
        flush=True,
    )
    box.close()


if __name__ == "__main__":
    for n in [int(x) for x in (sys.argv[1:] or ["100000", "500000"])]:
        probe(n)
