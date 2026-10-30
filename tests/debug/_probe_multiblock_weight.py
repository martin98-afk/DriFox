# -*- coding: utf-8 -*-
"""多块消息（content=list）专项：块数 / 类型 / HTML 体积 / lazy_body 收益。

真实库中绝大多数 assistant 消息 content 是 str（单块纯文本）；块级渲染的
收益上限只能由 list 内容的多块消息决定。全库扫描，只读。
输出英文。
"""

import os
import sqlite3
import sys
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

from app.core.store.serde import deserialize  # noqa: E402
from app.widgets.render import RenderCtx, render_message  # noqa: E402

DB = _ROOT / ".drifox" / "sessions.db"
CTX = RenderCtx(streaming=False, compact=False)
CTX_LAZY = RenderCtx(streaming=False, compact=False, lazy_body=True)


def pct(values, p):
    if not values:
        return 0
    return sorted(values)[min(len(values) - 1, int(len(values) * p))]


def human(n):
    return f"{n / 1024:.0f}KB" if n >= 1024 else f"{n}B"


def main():
    con = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)
    cur = con.execute("select messages from sessions where messages is not null")
    rows = []
    kind_bytes = {}
    kind_count = {}
    lazy_saved = 0
    total_bytes = 0
    for (raw,) in cur:
        try:
            messages = deserialize(raw)
        except Exception:
            continue
        for msg in messages if isinstance(messages, list) else []:
            if not isinstance(msg, dict):
                continue
            blocks = msg.get("content")
            if not isinstance(blocks, list) or not blocks:
                continue
            try:
                mr = render_message(blocks, CTX)
                total = sum(len(it.html) for it in (*mr.content, *mr.tool))
                mr_lazy = render_message(blocks, CTX_LAZY)
                lazy_total = sum(len(it.html) for it in (*mr_lazy.content, *mr_lazy.tool))
            except Exception:
                continue
            for it in (*mr.content, *mr.tool):
                n = len(it.html)
                kind_bytes[it.kind] = kind_bytes.get(it.kind, 0) + n
                kind_count[it.kind] = kind_count.get(it.kind, 0) + 1
            lazy_saved += max(0, total - lazy_total)
            total_bytes += total
            rows.append((total, len(blocks), msg.get("role"), [b.get("type") for b in blocks if isinstance(b, dict)]))
    con.close()

    n = len(rows)
    sizes = [r[0] for r in rows]
    counts = [r[1] for r in rows]
    print(f"list-content messages: {n}")
    if not n:
        return
    print(f"blocks/msg  p50 {pct(counts, 0.5)}  p90 {pct(counts, 0.9)}  p99 {pct(counts, 0.99)}  max {max(counts)}")
    print(f"html/msg    p50 {human(pct(sizes, 0.5))}  p90 {human(pct(sizes, 0.9))}  "
          f"p99 {human(pct(sizes, 0.99))}  max {human(max(sizes))}")
    print(f"html total  {human(total_bytes)}  lazy_body save potential {human(lazy_saved)} "
          f"({100.0 * lazy_saved / max(1, total_bytes):.1f}%)")
    print("\nbytes by kind:")
    for kind, b in sorted(kind_bytes.items(), key=lambda kv: -kv[1]):
        print(f"  {kind:10s} {human(b):>8s}  {100.0 * b / max(1, total_bytes):5.1f}%  "
              f"count {kind_count[kind]}  avg {human(b // max(1, kind_count[kind]))}")
    print("\ntop 10 heaviest list messages:")
    for total, nb, role, types in sorted(rows, key=lambda x: -x[0])[:10]:
        print(f"  {human(total):>7s} blocks={nb:<3d} role={role:<9s} {types}")
    print("\nmsgs > 256KB:", sum(1 for s in sizes if s > 262144), " > 1MB:", sum(1 for s in sizes if s > 1048576))


if __name__ == "__main__":
    main()
