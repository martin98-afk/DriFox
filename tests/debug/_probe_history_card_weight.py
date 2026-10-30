# -*- coding: utf-8 -*-
"""历史卡真实重量：assistant 消息 + 其后 tool 消息合成一张卡，测块数与 HTML 体积。

真实库的块结构不在 messages.content 里（多是 str），reasoning_content /
arguments / diff 被剥离到 session_msg_extras。历史卡渲染时这些字段被读回并
转成 reasoning / tool_result 块。本 probe 复现该合成口径，量化：

- 一张历史卡有多少块、多少 HTML 字节
- 其中工具块（lazy_body 已省 body）与正文/思考块各占多少
- lazy_body 还能再省多少（对照渲染）

只读。输出英文。
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

from app.core.conversation.message_content import ensure_content_blocks, make_tool_result_block  # noqa: E402
from app.core.store.serde import deserialize  # noqa: E402
from app.widgets.render import RenderCtx, render_message  # noqa: E402

DB = _ROOT / ".drifox" / "sessions.db"
LIMIT = int(os.environ.get("PROBE_SESSIONS", "60"))


def pct(values, p):
    if not values:
        return 0
    return sorted(values)[min(len(values) - 1, int(len(values) * p))]


def human(n):
    return f"{n / 1024:.0f}KB" if n >= 1024 else f"{n}B"


def load_extras(con, session_id):
    out = {}
    for idx, field, raw in con.execute(
        "select msg_idx, field, value from session_msg_extras where session_id=?", (session_id,)
    ):
        try:
            out.setdefault(idx, {})[field] = deserialize(raw)
        except Exception:
            continue
    return out


def group_cards(messages):
    """assistant 消息 + 其后连续 tool 消息 → (blocks, role) 列表。"""
    cards = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if not isinstance(msg, dict):
            i += 1
            continue
        role = msg.get("role")
        if role == "assistant":
            blocks = []
            reasoning = msg.get("reasoning_content")
            if reasoning:
                blocks.append({"type": "reasoning", "content": str(reasoning)})
            blocks.extend(ensure_content_blocks(msg.get("content")))
            j = i + 1
            name_by_id = {}
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    name_by_id[tc.get("id")] = (tc.get("function") or {}).get("name", "tool")
            while j < n and isinstance(messages[j], dict) and messages[j].get("role") == "tool":
                tm = messages[j]
                blocks.append(
                    make_tool_result_block(
                        tool_name=tm.get("name") or name_by_id.get(tm.get("tool_call_id"), "tool"),
                        arguments=tm.get("arguments") if isinstance(tm.get("arguments"), dict) else None,
                        result=tm.get("content"),
                        success=not bool(tm.get("is_error")),
                        tool_call_id=tm.get("tool_call_id"),
                        diff=tm.get("diff"),
                    )
                )
                j += 1
            if blocks:
                cards.append(blocks)
            i = j
        else:
            i += 1
    return cards


def main():
    con = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)
    rows = con.execute(
        "select session_id, messages from sessions where messages is not null order by length(messages) desc limit ?",
        (LIMIT,),
    ).fetchall()

    card_sizes = []
    card_blocks = []
    kind_bytes = {}
    kind_count = {}
    lazy_saved = 0
    total_bytes = 0
    ctx = RenderCtx(streaming=False, compact=False, lazy_body=True)
    ctx_nolazy = RenderCtx(streaming=False, compact=False, lazy_body=False)
    ctx_compact = RenderCtx(streaming=False, compact=True, lazy_body=True)

    for sid, raw in rows:
        try:
            messages = deserialize(raw)
        except Exception:
            continue
        if not isinstance(messages, list):
            continue
        extras = load_extras(con, sid)
        for idx, patch in extras.items():
            if 0 <= idx < len(messages) and isinstance(messages[idx], dict):
                messages[idx].update(patch)
        for blocks in group_cards(messages):
            try:
                mr = render_message(blocks, ctx)
                total = sum(len(it.html) for it in (*mr.content, *mr.tool))
                saved = total - sum(
                    len(it.html) for it in (*render_message(blocks, ctx_nolazy).content, *render_message(blocks, ctx_nolazy).tool)
                )
            except Exception:
                continue
            for it in (*mr.content, *mr.tool):
                nb = len(it.html)
                kind_bytes[it.kind] = kind_bytes.get(it.kind, 0) + nb
                kind_count[it.kind] = kind_count.get(it.kind, 0) + 1
            lazy_saved += max(0, saved)
            total_bytes += total
            card_sizes.append(total)
            card_blocks.append(len(blocks))
    con.close()

    n = len(card_sizes)
    print(f"history cards (assistant+tool merged): {n}")
    if not n:
        return
    print(f"blocks/card  p50 {pct(card_blocks, 0.5)}  p90 {pct(card_blocks, 0.9)}  "
          f"p99 {pct(card_blocks, 0.99)}  max {max(card_blocks)}")
    print(f"html/card    p50 {human(pct(card_sizes, 0.5))}  p90 {human(pct(card_sizes, 0.9))}  "
          f"p99 {human(pct(card_sizes, 0.99))}  max {human(max(card_sizes))}")
    print(f"html total   {human(total_bytes)}  lazy_body still can save {human(lazy_saved)} "
          f"({100.0 * lazy_saved / max(1, total_bytes):.1f}%)")
    print("\nbytes by kind (compact=False + lazy_body=True, the history-card form):")
    for kind, b in sorted(kind_bytes.items(), key=lambda kv: -kv[1]):
        print(f"  {kind:10s} {human(b):>8s}  {100.0 * b / max(1, total_bytes):5.1f}%  "
              f"count {kind_count[kind]}  avg {human(b // max(1, kind_count[kind]))}")
    for label, th in (("256KB", 262144), ("512KB", 524288), ("1MB", 1048576)):
        c = sum(1 for s in card_sizes if s > th)
        print(f"cards > {label}: {c} ({100.0 * c / n:.2f}%)")


if __name__ == "__main__":
    main()
