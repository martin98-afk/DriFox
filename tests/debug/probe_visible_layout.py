# -*- coding: utf-8 -*-
"""探针 v8：可见态 QTextEdit 装载超长文本后，Qt 自身的排版成本（offscreen）

背景：v6 显示 PlainTextViewer._update_height 的全量测量 500k=4362ms；
但真机上 QTextEdit 可见时，即使跳过测量，Qt 也会在 paint/几何索取时排版整篇。
本探针回答：卡顿是「我们的测量」造成的，还是「QTextDocument 排版 500k 文本」的
固有限制（后者无法靠跳测量解决）。

步骤：
  1. PlainTextViewer 正常 cap=900，set_text 500k，show + processEvents（模拟上屏）
  2. 强制 grab() 促发真实 paint
  3. 分别计时，看总卡顿时长

运行：
  $env:QT_QPA_PLATFORM="offscreen"; python -u tests\debug\probe_visible_layout.py
"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DRIFOX_NO_CODEBUDDY_REFRESH", "1")
os.environ.setdefault("DRIFOX_DATA_DIR", "D:\\work\\DriFox\\.drifox-test")

from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from PyQt5.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from app.widgets.card_viewers import PlainTextViewer  # noqa: E402


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
    print(f"\n===== 可见态 {n_chars} 字符 =====", flush=True)
    text = make_text(n_chars)

    v = PlainTextViewer()
    v.set_width_cap(900)
    v.resize(900, 300)
    v.show()
    app.processEvents()

    _, t_set = ms(lambda: v.set_text(text))
    print(f"  set_text（含 _update_height）      {t_set:9.2f} ms", flush=True)

    _, t_pe = ms(lambda: app.processEvents())
    print(f"  随后 processEvents                 {t_pe:9.2f} ms", flush=True)

    _, t_grab = ms(lambda: v.grab())
    print(f"  grab()（强制真实 paint）           {t_grab:9.2f} ms", flush=True)

    _, t_grab2 = ms(lambda: v.grab())
    print(f"  grab() 二次                        {t_grab2:9.2f} ms", flush=True)

    doc = v.text_edit.document()
    _, t_size = ms(lambda: doc.size())
    print(f"  doc.size()                        {t_size:9.2f} ms", flush=True)

    # 滚动一口气：模拟用户拖内部滚动条
    vsb = v.text_edit.verticalScrollBar()
    _, t_scroll = ms(lambda: vsb.setValue(vsb.maximum()))
    _, t_pe2 = ms(lambda: app.processEvents())
    print(f"  滚到底 + processEvents             {t_scroll + t_pe2:9.2f} ms", flush=True)

    v.cleanup()
    v.close()


if __name__ == "__main__":
    for n in [int(x) for x in (sys.argv[1:] or ["100000", "300000"])]:
        probe(n)
