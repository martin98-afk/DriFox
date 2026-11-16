# -*- coding: utf-8 -*-
"""探针 v6：用户卡片 set_text / finish_streaming 的 4.2s 落在哪个分支（offscreen）

v5 实测（probe_send_pipeline）：100k 字符 finish_streaming=176ms，500k=4175ms，
而 update_content（含 viewer.set_text）只有 7.6ms。需要确认：
  1. set_text 里 text_edit.setPlainText 的布局是同步还是延迟到 finish 触发
  2. _update_height 走的是 O(1) 快速路径还是全量文档测量

手段：monkeypatch PlainTextViewer._update_height 打点（不改源码）。

运行：
  $env:QT_QPA_PLATFORM="offscreen"; python -u tests\debug\probe_card_finish_branch.py
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

from app.widgets import card_viewers as cv  # noqa: E402

STATS = {"calls": 0, "total_ms": 0.0, "fast": 0, "full": 0, "incr": 0}


def ms(fn):
    t0 = time.perf_counter()
    r = fn()
    return r, (time.perf_counter() - t0) * 1000


def patch():
    orig = cv.PlainTextViewer._update_height

    def wrapped(self, incremental: bool = False):
        STATS["calls"] += 1
        t0 = time.perf_counter()
        try:
            return orig(self, incremental)
        finally:
            dt = (time.perf_counter() - t0) * 1000
            STATS["total_ms"] += dt
            if incremental:
                STATS["incr"] += 1
            if dt > 20:
                # 慢调用：记录当时分支判据
                cap = self._width_cap
                print(
                    f"    [慢 _update_height] {dt:9.2f} ms incremental={incremental} "
                    f"cap={cap} tall_cap={self._tall_cap} len={len(self._text)} "
                    f"阈值={self._definitely_tall_chars()}",
                    flush=True,
                )
            else:
                STATS["fast"] += 1

    cv.PlainTextViewer._update_height = wrapped


def make_text(n_chars: int, line_len: int = 80) -> str:
    unit = "这是一段用于测试发送卡顿的超长文本内容 abcdefghij"
    lines, total = [], 0
    while total < n_chars:
        lines.append((unit * (line_len // len(unit) + 2))[:line_len])
        total += line_len + 1
    return "\n".join(lines)


def probe(n_chars: int):
    from app.widgets.message_card import MessageCard

    print(f"\n===== {n_chars} 字符 =====", flush=True)
    STATS.update(calls=0, total_ms=0.0, fast=0, full=0, incr=0)
    text = make_text(n_chars)

    card = MessageCard(parent=None, role="user")
    card.resize(900, 400)
    card.show()
    app.processEvents()

    _, t_upd = ms(lambda: card.update_content(text))
    print(f"  update_content        {t_upd:9.2f} ms", flush=True)

    # update 之后立刻查文档布局是否已就绪（是否延迟）
    v = card.viewer
    if v is not None:
        _, t_doc = ms(lambda: v.text_edit.document().size())
        print(f"  doc.size() (update 后) {t_doc:9.2f} ms", flush=True)
        print(
            f"    文档 blocks={v.text_edit.document().blockCount()} "
            f"textWidth={v.text_edit.document().textWidth():.0f} "
            f"viewport={v.text_edit.viewport().width()}",
            flush=True,
        )

    _, t_fin = ms(lambda: card.finish_streaming())
    print(f"  finish_streaming      {t_fin:9.2f} ms", flush=True)
    print(
        f"  _update_height 调用 {STATS['calls']} 次，合计 {STATS['total_ms']:.2f} ms "
        f"(增量 {STATS['incr']} / 快 {STATS['fast']})",
        flush=True,
    )
    card.close()


if __name__ == "__main__":
    patch()
    for n in [int(x) for x in (sys.argv[1:] or ["100000", "500000"])]:
        probe(n)
