# -*- coding: utf-8 -*-
"""B5.2 纯文本尾态收益 + 内容类型覆盖实测。

对真实库的长消息按内容类型抽样（代码块 / 表格 / 列表 / 公式 / think / 纯文本），
模拟流式（40 字符/批、假时钟 0.4s/批）跑真实 ``_perform_update_v2``：

- legacy：live 关闭（_LIVE_MIN_CHARS 调到极大）→ 每批全量 markdown + 整块重传
- live：纯文本尾态 + 增量 append + 边界正式化（真跑节流）

同时校验：
- 正式化只在围栏闭合后发生（半截代码块不得正式化）
- 最终下发覆盖全文（无内容丢失）
- 正式化次数（观感：多久格式化一次）

输出英文。
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

from app.core.store.serde import deserialize  # noqa: E402
from app.widgets.card_viewers import CodeWebViewer  # noqa: E402
from app.widgets.render import BlockCache, RenderCtx  # noqa: E402

DB = _ROOT / ".drifox" / "sessions.db"
CHUNK = 40
MIN_LEN = 3000


class _Page:
    def __init__(self):
        self.calls = []

    def runJavaScript(self, js, callback=None):
        self.calls.append(js)


class _Card:
    def __init__(self, blocks):
        self._content_data = blocks


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def advance(self, dt=0.4):
        self.t += dt

    def monotonic(self):
        return self.t


def make_viewer(blocks, page):
    v = CodeWebViewer.__new__(CodeWebViewer)
    v._is_js_ready = True
    v._streaming = True
    v._light_skeleton = True
    v._dock_sync_requested = True
    v._render_v2 = True
    v._block_cache = BlockCache()
    v._v2_last = None
    v._v2_ctx_sig = None
    v._v2_live_key = ""
    v._v2_live_sent = ""
    v._v2_live_formalized_len = 0
    v._v2_live_formalize_ts = 0.0
    v._markdown_text = "md"
    v._lazy_markdown_cb = None
    v._needs_full_render = True
    v._final_render_pending = False
    v._height_report_pending = False
    v._has_charts = False
    v._last_rendered_markdown = ""
    v._render_deferred = False
    v._page = page
    v.page = lambda: page
    v.isVisible = lambda: True
    v.parent = lambda: _Card(blocks)
    v._sync_streaming_dock = lambda *a, **k: None
    v._v2_render_ctx = lambda: RenderCtx(streaming=True, compact=False)
    return v


def load_samples():
    con = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)
    buckets = {"code": [], "table": [], "list": [], "formula": [], "think": [], "plain": []}
    for (raw,) in con.execute("select messages from sessions where messages is not null"):
        try:
            messages = deserialize(raw)
        except Exception:
            continue
        for msg in messages if isinstance(messages, list) else []:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            c = msg.get("content")
            if not isinstance(c, str) or len(c) <= MIN_LEN:
                continue
            buckets["plain"].append(c)
            if "```" in c:
                buckets["code"].append(c)
            if "\n|" in c:
                buckets["table"].append(c)
            if "\n- " in c or "\n1. " in c:
                buckets["list"].append(c)
            if "$" in c:
                buckets["formula"].append(c)
            if "<think>" in c:
                buckets["think"].append(c)
    con.close()
    out = []
    for kind, items in buckets.items():
        if items:
            items.sort(key=len, reverse=True)
            out.append((kind, items[0]))
    return out


def run(text, live: bool):
    import app.widgets.card_viewers as cv

    clock = _Clock()
    real_time = cv.time
    cv.time = clock
    old_min = CodeWebViewer._LIVE_MIN_CHARS
    if not live:
        CodeWebViewer._LIVE_MIN_CHARS = 10**9
    page = _Page()
    blocks = [{"type": "text", "text": ""}]
    v = make_viewer(blocks, page)
    total_js = 0
    formalizes = 0
    bad_formalize = 0
    expect_formal = False
    t0 = time.perf_counter()
    try:
        pos = 0
        while pos < len(text):
            pos = min(len(text), pos + CHUNK)
            blocks[0]["text"] = text[:pos]
            v._needs_full_render = v._v2_last is None
            page.calls.clear()
            v._perform_update_v2()
            js = "".join(page.calls)
            total_js += len(js)
            if live and "patchBlocks('content-placeholder'" in js and "_c1.innerHTML=''" not in js:
                # 非首帧的整块替换 = 正式化（live 下活动块 identity 恒定，只有正式化会替换它）
                formalizes += 1
                if text[:pos].count("```") % 2:
                    bad_formalize += 1
            clock.advance()
    finally:
        cv.time = real_time
        CodeWebViewer._LIVE_MIN_CHARS = old_min
    return {
        "js": total_js,
        "ms": (time.perf_counter() - t0) * 1000,
        "formalizes": formalizes,
        "bad_formalize": bad_formalize,
        "sent": len(v._v2_live_sent),
        "total": len(text),
        "expect_formal": expect_formal,
    }


def main():
    for kind, text in load_samples():
        old = run(text, live=False)
        new = run(text, live=True)
        print(
            f"\n[{kind}] len={len(text)} fences={text.count('```')}\n"
            f"  legacy  js={old['js'] / 1048576:7.2f}MB  python={old['ms']:7.0f}ms\n"
            f"  live    js={new['js'] / 1048576:7.2f}MB  python={new['ms']:7.0f}ms  "
            f"formalizes={new['formalizes']} bad={new['bad_formalize']} "
            f"coverage={new['sent']}/{new['total']}  "
            f"(js x{old['js'] / max(1, new['js']):.0f}, cpu x{old['ms'] / max(1, new['ms']):.0f})"
        )


if __name__ == "__main__":
    main()
