# -*- coding: utf-8 -*-
"""真实语料渲染耗时分布：渲染到底占多少时间。

目的：判断 block 级重构的性能收益上限。如果真实消息小到渲染只要几毫秒，
那么卡顿与内存问题必然不在渲染 CPU 上，而在 WebEngine 实例数与状态机 bug。
"""
import os
import sqlite3
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("DRIFOX_DATA_DIR", str(_ROOT / ".drifox-test"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["DRIFOX_QT_RENDERER"] = "1"

from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from app.core.conversation.message_content import content_to_markdown, ensure_content_blocks  # noqa: E402
from app.core.store.serde import deserialize  # noqa: E402
from app.widgets.card_render_core import _render_markdown_to_html_cached_impl  # noqa: E402

_raw = _render_markdown_to_html_cached_impl.__wrapped__
DB = _ROOT / ".drifox" / "sessions.db"


def main():
    con = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)
    cur = con.cursor()
    cur.execute("select messages from sessions where messages is not null order by length(messages) desc limit 80")
    timings = []
    md_lens = []
    for (raw,) in cur.fetchall():
        try:
            messages = deserialize(raw)
        except Exception:
            continue
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            blocks = ensure_content_blocks(msg.get("content"))
            if not blocks:
                continue
            md = content_to_markdown(blocks)
            if not md:
                continue
            t0 = time.perf_counter()
            _raw(md, compact=False)
            timings.append((time.perf_counter() - t0) * 1000)
            md_lens.append(len(md))
    con.close()

    timings.sort()
    n = len(timings)
    if not n:
        print("no data")
        return
    pct = lambda p: timings[min(n - 1, int(n * p))]  # noqa: E731
    print(f"样本 {n} 条 assistant 消息（真实库）")
    print(f"md 长度  p50 {sorted(md_lens)[n // 2]}  p90 {sorted(md_lens)[int(n * 0.9)]}  max {max(md_lens)}")
    print(f"渲染耗时 p50 {pct(0.5):.2f}ms  p75 {pct(0.75):.2f}ms  p90 {pct(0.9):.2f}ms  p99 {pct(0.99):.2f}ms  max {timings[-1]:.2f}ms")
    total = sum(timings)
    print(f"全部重渲一遍合计 {total:.0f}ms")


if __name__ == "__main__":
    main()
