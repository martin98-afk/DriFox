# -*- coding: utf-8 -*-
"""临时探针：超长文本「发送」链路分段计时（offscreen）

目的：定位「发送超长文本整个软件卡死」的主线程阻塞段。
分段：
  A. 输入框 setPlainText（粘贴等价）
  B. 输入框 textChanged 链（_on_text_changed / slash / at / 高亮）
  C. PlainTextViewer.set_text（用户气泡渲染）
  D. PlainTextViewer 高度重算（含 _definitely_tall_chars 快速路径）

运行：
  set QT_QPA_PLATFORM=offscreen && uv run python tests/debug/probe_long_text_send_hang.py
"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DRIFOX_NO_CODEBUDDY_REFRESH", "1")

from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402

# QtWebEngineWidgets 要求在任何 QCoreApplication 之前设置
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from PyQt5.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from app.widgets.bottom_input_area import SendableTextEdit  # noqa: E402
from app.widgets.card_viewers import PlainTextViewer  # noqa: E402


def make_text(n_chars: int, line_len: int = 80) -> str:
    """构造近似真实的长文本：定长行 + 换行"""
    unit = "这是一段用于测试发送卡顿的超长文本内容 abcdefghij"
    lines = []
    total = 0
    while total < n_chars:
        line = (unit * (line_len // len(unit) + 2))[:line_len]
        lines.append(line)
        total += line_len + 1
    return "\n".join(lines)


def timeit(label, fn):
    t0 = time.perf_counter()
    r = fn()
    dt = (time.perf_counter() - t0) * 1000
    print(f"  {label:<34} {dt:10.2f} ms")
    return r, dt


def probe_input_box(n_chars: int):
    print(f"\n=== 输入框 setPlainText  {n_chars} 字符 ===")
    box = SendableTextEdit()
    box.resize(900, 120)
    box._initializing = False
    box.show()
    app.processEvents()

    text = make_text(n_chars)
    timeit("setPlainText(原始)", lambda: box.setPlainText(text))
    app.processEvents()
    timeit("toPlainText()", lambda: box.toPlainText())
    timeit("document().size()", lambda: box.document().size())
    timeit("_adjust_height_to_content", lambda: box._adjust_height_to_content())

    # 高亮器：粘贴后重扫全部 block
    hl = getattr(box, "_placeholder_highlighter", None)
    if hl is not None:
        timeit("highlighter.rehighlight()", lambda: hl.rehighlight())
    box.close()
    return


def probe_plain_viewer(n_chars: int):
    print(f"\n=== 用户气泡 PlainTextViewer  {n_chars} 字符 ===")
    v = PlainTextViewer()
    v.set_width_cap(900)
    v.show()
    app.processEvents()

    text = make_text(n_chars)
    _, t_set = timeit("set_text(含同步高度)", lambda: v.set_text(text))
    timeit("二次 set_text(同内容)", lambda: v.set_text(text))
    timeit("update_height()", lambda: v.update_height())
    print(f"  -> _tall_cap={v._tall_cap} 阈值={v._definitely_tall_chars()} 高度={v.height()}")
    v.cleanup()
    v.close()
    return t_set


if __name__ == "__main__":
    sizes = [int(x) for x in (sys.argv[1:] or ["10000", "100000", "500000"])]
    for n in sizes:
        probe_input_box(n)
        probe_plain_viewer(n)
