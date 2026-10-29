# -*- coding: utf-8 -*-
"""定位渲染慢的少数消息：哪些内容让单条渲染耗时上百毫秒。

说明：本脚本只读会话库，输出慢消息的特征（长度 / 结构 / 触发的重活）。
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


def features(md: str) -> str:
    tags = []
    if "```" in md:
        tags.append(f"fence×{md.count('```') // 2}")
    if "<think>" in md:
        tags.append(f"think×{md.count('<think>')}")
    if "<tool>" in md:
        tags.append(f"tool×{md.count('<tool>')}")
    if "$$" in md or "\\[" in md:
        tags.append("latex")
    if "![" in md:
        tags.append(f"img×{md.count('![')}")
    if "|" in md and "---" in md:
        tags.append("table?")
    return ",".join(tags) or "-"


def main():
    con = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)
    cur = con.cursor()
    cur.execute("select messages from sessions where messages is not null order by length(messages) desc limit 80")
    rows = []
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
            ms = (time.perf_counter() - t0) * 1000
            rows.append((ms, len(md), len(blocks), features(md)))
    con.close()

    rows.sort(reverse=True)
    print("最慢 15 条：")
    for ms, ln, nb, feat in rows[:15]:
        print(f"  {ms:8.2f}ms  len={ln:6d}  blocks={nb}  {feat}")

    slow = [r for r in rows if r[0] > 50]
    print(f"\n>50ms 的条数: {len(slow)} / {len(rows)}  (合计 {sum(r[0] for r in slow):.0f}ms)")
    print(f"全部合计: {sum(r[0] for r in rows):.0f}ms")


if __name__ == "__main__":
    main()
