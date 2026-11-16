# -*- coding: utf-8 -*-
"""探针 v3：textChanged 槽链在长文本 setPlainText 时的触发次数与逐槽耗时（offscreen）

v2 结论：SetPlainText 500k 字符 4579ms，blockSignals 后 19.78ms → 卡在槽链，
与高亮器无关（摘掉仍 4509ms）。

本探针回答：textChanged 触发几次、每个槽各占多少

运行：
  $env:QT_QPA_PLATFORM="offscreen"; python -u tests\debug\probe_text_changed_chain.py
"""

import os
import sys
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DRIFOX_NO_CODEBUDDY_REFRESH", "1")

from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

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


class Counter:
    def __init__(self, name, fn):
        self.name, self.fn, self.n, self.t = name, fn, 0, 0.0
        self.max_t = 0.0

    def __call__(self, *a, **kw):
        t0 = time.perf_counter()
        r = self.fn(*a, **kw)
        dt = (time.perf_counter() - t0) * 1000
        self.n += 1
        self.t += dt
        self.max_t = max(self.max_t, dt)
        return r


def probe(n_chars: int):
    print(f"\n===== {n_chars} 字符 =====", flush=True)
    box = SendableTextEdit()
    box.resize(900, 120)
    box._initializing = False
    box.show()
    app.processEvents()

    counters = []
    # textChanged 三连槽
    for name in ("_on_text_changed", "_on_slash_trigger_check", "_on_at_trigger_check"):
        orig = getattr(box, name)
        c = Counter(name, orig)
        # 断开原连接再连包装（PyQt：disconnect(bound) 不可靠，用 blockSignals 前先摘）
        try:
            box.textChanged.disconnect(orig)
        except TypeError:
            pass
        box.textChanged.connect(c)
        counters.append(c)

    text = make_text(n_chars)
    t0 = time.perf_counter()
    box.setPlainText(text)
    total = (time.perf_counter() - t0) * 1000

    print(f"  setPlainText 总耗时 {total:.2f} ms", flush=True)
    for c in counters:
        print(
            f"    {c.name:<26} 触发 {c.n:5d} 次  合计 {c.t:9.2f} ms  单次最大 {c.max_t:8.2f} ms",
            flush=True,
        )
    box.close()


if __name__ == "__main__":
    for n in [int(x) for x in (sys.argv[1:] or ["10000", "100000"])]:
        probe(n)
