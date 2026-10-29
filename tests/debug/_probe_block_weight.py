# -*- coding: utf-8 -*-
"""真实语料 block 权重分解：量化「块级惰性渲染」的收益上限。

回答一个问题：一张卡片里，用户看不到的块到底占多少 HTML 字节？
- 若单卡 HTML 体积本来就只有数百 KB、且块数 ≤ 11 → 块级虚拟化收益极小，
  该把力气放在别处（卡片级卸载 / 首屏延迟渲染）。
- 若存在大量超重单卡（>1MB） → 块级懒渲染有明确价值。

只读 sessions.db（mode=ro）。输出一律英文（避免 Windows 控制台编码问题）。
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

from app.core.conversation.message_content import ensure_content_blocks  # noqa: E402
from app.core.store.serde import deserialize  # noqa: E402
from app.widgets.render import RenderCtx, render_message  # noqa: E402

DB = _ROOT / ".drifox" / "sessions.db"
LIMIT_SESSIONS = int(os.environ.get("PROBE_SESSIONS", "80"))
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
    cur = con.cursor()
    cur.execute(
        "select messages from sessions where messages is not null order by length(messages) desc limit ?",
        (LIMIT_SESSIONS,),
    )

    msg_count = 0
    block_counts = []
    html_sizes = []
    kind_bytes = {}
    kind_count = {}
    pre_bytes = 0
    total_bytes = 0
    lazy_saved = 0
    heavy = []  # (total_bytes, n_blocks, breakdown)

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
            try:
                mr = render_message(blocks, CTX)
            except Exception:
                continue
            msg_count += 1
            block_counts.append(len(blocks))
            total = 0
            breakdown = []
            for it in (*mr.content, *mr.tool):
                n = len(it.html)
                total += n
                kind_bytes[it.kind] = kind_bytes.get(it.kind, 0) + n
                kind_count[it.kind] = kind_count.get(it.kind, 0) + 1
                pre_bytes += it.html.count("<pre")
                breakdown.append((it.kind, n))
            # lazy_body 能省下的工具块字节（对照：同样块集，lazy_body=True）
            try:
                mr_lazy = render_message(blocks, CTX_LAZY)
                lazy_total = sum(len(it.html) for it in (*mr_lazy.content, *mr_lazy.tool))
                lazy_saved += max(0, total - lazy_total)
            except Exception:
                pass
            html_sizes.append(total)
            total_bytes += total
            heavy.append((total, len(blocks), breakdown))
    con.close()

    print(f"sessions scanned: top {LIMIT_SESSIONS} by size; assistant messages: {msg_count}")
    if not msg_count:
        print("no data")
        return
    print(f"blocks/msg   p50 {pct(block_counts, 0.5)}  p90 {pct(block_counts, 0.9)}  "
          f"p99 {pct(block_counts, 0.99)}  max {max(block_counts)}")
    print(f"html/msg     p50 {human(pct(html_sizes, 0.5))}  p90 {human(pct(html_sizes, 0.9))}  "
          f"p99 {human(pct(html_sizes, 0.99))}  max {human(max(html_sizes))}")
    print(f"html total   {human(total_bytes)} across {msg_count} msgs  (avg {human(total_bytes // msg_count)})")
    print(f"lazy_body save potential: {human(lazy_saved)} "
          f"({100.0 * lazy_saved / max(1, total_bytes):.1f}% of html)")
    print("\nbytes by kind:")
    for kind, n in sorted(kind_bytes.items(), key=lambda kv: -kv[1]):
        print(f"  {kind:10s} {human(n):>8s}  {100.0 * n / max(1, total_bytes):5.1f}%  "
              f"count {kind_count[kind]}  avg {human(n // max(1, kind_count[kind]))}")
    print("\ntop 10 heaviest messages:")
    for total, nb, breakdown in sorted(heavy, key=lambda x: -x[0])[:10]:
        parts = " ".join(f"{k}:{human(v)}" for k, v in sorted(breakdown, key=lambda x: -x[1])[:6])
        print(f"  {human(total):>7s}  blocks={nb:<3d} {parts}")
    over_1m = sum(1 for s in html_sizes if s > 1024 * 1024)
    over_256k = sum(1 for s in html_sizes if s > 256 * 1024)
    print(f"\nmsgs > 256KB: {over_256k} ({100.0 * over_256k / msg_count:.1f}%)   "
          f"> 1MB: {over_1m} ({100.0 * over_1m / msg_count:.1f}%)")


if __name__ == "__main__":
    main()
