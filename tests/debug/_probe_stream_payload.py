# -*- coding: utf-8 -*-
"""流式 patch 传输量：v2 每批向 JS 传多少字节，量化 O(n²) 程度。

真实库取最长的几条 assistant 文本消息，按 40 字符/批模拟流式：
- 每批调用 render_message + diff_message，统计 patchBlocks payload 的 JSON 字节
- 对照：旧路径纯文本追加的理论传输量（每批 40 字符）
- 同时统计每批 Python 渲染耗时（活动块每批重渲，块缓存不命中）

结论用途：判断「活动块流式降载」是否值得做。
输出英文。
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

from app.core.store.serde import deserialize  # noqa: E402
from app.widgets.render import BlockCache, RenderCtx, diff_message, render_message  # noqa: E402

DB = _ROOT / ".drifox" / "sessions.db"
CHUNK = 40
MAX_MSGS = int(os.environ.get("PROBE_MSGS", "8"))
CTX = RenderCtx(streaming=True, compact=False)


def main():
    con = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)
    con.text_factory = str
    # 找最长的纯文本 assistant 消息
    best = []
    for (raw,) in con.execute("select messages from sessions where messages is not null"):
        try:
            messages = deserialize(raw)
        except Exception:
            continue
        for msg in messages if isinstance(messages, list) else []:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            c = msg.get("content")
            if isinstance(c, str) and len(c) > 4000:
                best.append(c)
    con.close()
    best.sort(key=len, reverse=True)
    samples = best[:MAX_MSGS]
    print(f"texts found: {len(best)}  using {len(samples)}")
    if not samples:
        return

    for text in samples:
        results = []
        for min_delta in (0, 80, 160, 240, 480):
            blocks = [{"type": "text", "text": ""}]
            cache = BlockCache()
            prev = None
            payload_bytes = 0
            payload_sizes = []
            render_ms = 0.0
            batches = 0
            pending = 0
            last_shown = 0
            for i in range(0, len(text), CHUNK):
                shown = min(i + CHUNK, len(text))
                pending = shown - last_shown
                if min_delta and pending < min_delta and shown < len(text):
                    continue
                last_shown = shown
                blocks[0]["text"] = text[:shown]
                t0 = time.perf_counter()
                mr = render_message(blocks, CTX, cache=cache)
                c_ops, t_ops = diff_message(prev, mr)
                prev = mr
                render_ms += (time.perf_counter() - t0) * 1000
                batch_payload = 0
                if not c_ops.is_empty:
                    batch_payload += len(json.dumps(c_ops.to_payload()).encode("utf-8"))
                if not t_ops.is_empty:
                    batch_payload += len(json.dumps(t_ops.to_payload()).encode("utf-8"))
                payload_bytes += batch_payload
                payload_sizes.append(batch_payload)
                batches += 1
            naive = len(text)  # 旧路径纯文本追加理论量
            p50 = sorted(payload_sizes)[len(payload_sizes) // 2] if payload_sizes else 0
            results.append(
                f"delta>={min_delta:<4d} batches={batches:>4d} payload={payload_bytes / 1024:>7.0f}KB "
                f"(x{payload_bytes / max(1, naive):>4.0f}) p50batch={p50 / 1024:>5.1f}KB python={render_ms:>6.0f}ms"
            )
        print(f"\nlen={len(text)} (naive-append {len(text) / 1024:.0f}KB)")
        for line in results:
            print("  " + line)


if __name__ == "__main__":
    main()
