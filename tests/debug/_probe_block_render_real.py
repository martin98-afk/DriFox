# -*- coding: utf-8 -*-
"""真实会话语料对照：用库里历史消息喂新旧两条管线，比较可见文本。

只读 sessions.db（mode=ro），不写任何东西。用于在步骤 1 验收「新 block 管线
与旧整串管线产物等价」，语料覆盖真实的长消息、多工具、思考块、代码块。
"""

import json
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

from app.core.conversation.message_content import content_to_markdown, ensure_content_blocks  # noqa: E402
from app.widgets.card_render_core import _render_markdown_to_html_cached  # noqa: E402
from app.widgets.render import RenderCtx, render_message, visible_text  # noqa: E402

DB = _ROOT / ".drifox" / "sessions.db"
LIMIT_SESSIONS = int(os.environ.get("PROBE_SESSIONS", "25"))
CTX = RenderCtx(streaming=False, compact=False)


def load_messages():
    from app.core.store.serde import deserialize

    con = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)
    cur = con.cursor()
    cur.execute(
        "select session_id, messages from sessions where messages is not null order by length(messages) desc limit ?",
        (LIMIT_SESSIONS,),
    )
    for sid, raw in cur.fetchall():
        try:
            yield sid, deserialize(raw)
        except Exception:
            continue
    con.close()


def main():
    total = 0
    mismatched = []
    for sid, messages in load_messages():
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") not in ("assistant", "user"):
                continue
            blocks = ensure_content_blocks(msg.get("content"))
            if not blocks:
                continue
            total += 1
            try:
                old_html = _render_markdown_to_html_cached(content_to_markdown(blocks), compact=False)
            except Exception as e:
                mismatched.append((sid, "OLD-ERR", str(e)[:120], ""))
                continue
            try:
                new = render_message(blocks, CTX)
                new_html = "\n".join(it.html for it in (*new.content, *new.tool))
            except Exception as e:
                mismatched.append((sid, "NEW-ERR", str(e)[:120], ""))
                continue
            ot, nt = visible_text(old_html), visible_text(new_html)
            if ot != nt:
                # 找第一处差异定位
                i = next((k for k in range(min(len(ot), len(nt))) if ot[k] != nt[k]), min(len(ot), len(nt)))
                mismatched.append((sid, "DIFF", ot[max(0, i - 60) : i + 60], nt[max(0, i - 60) : i + 60]))
    print(f"messages compared: {total}, mismatched: {len(mismatched)}")
    for row in mismatched[:12]:
        print("-" * 70)
        for cell in row:
            print(repr(cell)[:260])


if __name__ == "__main__":
    main()
