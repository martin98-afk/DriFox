# -*- coding: utf-8 -*-
"""性能对照：旧整串管线 vs 新 block 管线（全量 + 流式增量）。

只读会话库取真实长消息。输出主线程可见的耗时差，用于判断步骤 1 的必要性。
"""

import json
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
from app.widgets.card_render_core import _render_markdown_to_html_cached_impl  # noqa: E402

_raw_render = _render_markdown_to_html_cached_impl.__wrapped__
from app.widgets.render import BlockCache, RenderCtx, render_message  # noqa: E402

DB = _ROOT / ".drifox" / "sessions.db"


def load_long_message(min_len=600):
    """选 block 数最多的消息（多工具 / 多思考块，最接近真实长回复）。

    真实库里绝大多数消息是单块纯文本，多块消息才是 block 管线的目标场景，
    因此优先按块数排序，同块数再比 markdown 长度。
    """
    from app.core.store.serde import deserialize

    con = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)
    cur = con.cursor()
    cur.execute("select messages from sessions where messages is not null order by length(messages) desc limit 200")
    best = None
    best_score = -1
    for (raw,) in cur.fetchall():
        try:
            messages = deserialize(raw)
        except Exception:
            continue
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            blocks = ensure_content_blocks(msg.get("content"))
            if len(blocks) < 2:
                continue
            md = content_to_markdown(blocks)
            if len(md) < min_len:
                continue
            score = len(blocks) * 100000 + len(md)
            if score > best_score:
                best_score = score
                best = (blocks, md)
    con.close()
    return best


def bench(fn, rounds=5):
    fn()
    ts = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    return min(ts), sum(ts) / len(ts)


def main():
    best = load_long_message()
    if not best:
        print("no long message found")
        return
    blocks, md = best
    print(f"blocks={len(blocks)}  md_len={len(md)}")

    ctx_done = RenderCtx(streaming=False, compact=False)
    ctx_stream = RenderCtx(streaming=True, compact=False)

    o_min, o_avg = bench(lambda: _raw_render(md, compact=False))
    n_min, n_avg = bench(lambda: render_message(blocks, ctx_done))
    print(f"全量渲染  旧: min {o_min:.1f}ms avg {o_avg:.1f}ms")
    print(f"全量渲染  新: min {n_min:.1f}ms avg {n_avg:.1f}ms")

    # 流式：最后一块持续增长，模拟 20 批
    tail = dict(blocks[-1]) if blocks[-1].get("type") == "text" else {"type": "text", "text": ""}
    head = blocks[:-1] if blocks[-1].get("type") == "text" else list(blocks)
    strides = ["词" * 30] * 20
    buf = ""

    def old_stream():
        nonlocal buf
        buf = ""
        for s in strides:
            buf += s
            _raw_render(content_to_markdown([*head, {"type": "text", "text": buf}]), compact=False)

    def new_stream():
        nonlocal buf
        buf = ""
        cache = BlockCache()
        for s in strides:
            buf += s
            render_message([*head, {"type": "text", "text": buf}], ctx_stream, cache=cache)
        return cache

    o_min, o_avg = bench(old_stream, rounds=2)
    n_min, n_avg = bench(new_stream, rounds=2)
    print(f"流式 20 批  旧:       min {o_min:.1f}ms avg {o_avg:.1f}ms")
    print(f"流式 20 批  新(无缓存): min {n_min:.1f}ms avg {n_avg:.1f}ms")

    def new_stream_cached():
        nonlocal buf
        buf = ""
        cache = BlockCache()
        for s in strides:
            buf += s
            render_message([*head, {"type": "text", "text": buf}], ctx_stream, cache=cache)
        return cache

    c_min, c_avg = bench(new_stream_cached, rounds=3)
    hit = new_stream_cached().stats
    print(f"流式 20 批  新(缓存):  min {c_min:.1f}ms avg {c_avg:.1f}ms  {hit}")


if __name__ == "__main__":
    main()
