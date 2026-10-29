# -*- coding: utf-8 -*-
"""v2 渲染通道：流式过程中「容器被整页清空」与「大范围块替换」的实测探针。

用户症状：流式输出时用户把卡内滚轮滚到上面，下一批更新自动跳回顶部；
v1（差量/全量 + save-restore）无此问题。

可疑机制：
  A. force_full（``_c1.innerHTML=''`` 清空 content-placeholder）→ 滚动容器
     scrollTop 被浏览器强制归 0。v2 通道没有任何 scrollTop 保存/恢复。
  B. compact 模式 ``_v2AfterPatch`` 把正文里的 think/tool 卡搬到工具区，
     视口上方内容被抽走 → scrollHeight 收缩 → scrollTop 被 clamp。

本探针只统计 Python 侧下发的 JS（stub page），不依赖 WebEngine：

  per batch: force_full? content_ops(delete/items) tool_ops(delete/items)

输出英文。
"""

import os
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

from app.widgets.card_viewers import CodeWebViewer  # noqa: E402
from app.widgets.render import BlockCache, RenderCtx  # noqa: E402

CHUNK = 200

class _Page:
    def __init__(self):
        self.calls = []

    def runJavaScript(self, js, callback=None):
        self.calls.append(js)

class _Card:
    def __init__(self, blocks):
        self._content_data = blocks

def make_viewer(blocks, page, ctx_getter):
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
    v._v2_render_ctx = ctx_getter
    v._mark_has_charts = lambda *a, **k: None
    return v

def run(compact: bool, use_live: bool):
    import app.widgets.card_viewers as cv

    text_a = "这是第一段回答。" * 120       # 正文块（长）
    text_b = "第二个问题的答案。" * 200
    blocks = [
        {"type": "reasoning", "content": "先想一下要怎么做。" * 80},
        {"type": "text", "text": ""},
        {"type": "tool_result", "tool_call_id": "c1", "name": "read_file", "success": True, "result": "ok"},
        {"type": "text", "text": ""},
    ]
    state = {"fps": frozenset()}

    def ctx_getter():
        return RenderCtx(streaming=True, compact=compact, tool_fingerprints=state["fps"])

    page = _Page()
    v = make_viewer(blocks, page, ctx_getter)
    old_min = CodeWebViewer._LIVE_MIN_CHARS
    if not use_live:
        CodeWebViewer._LIVE_MIN_CHARS = 10**9

    events = []
    try:
        def batch(tag):
            page.calls.clear()
            v._needs_full_render = v._v2_last is None
            v._perform_update_v2()
            js = "".join(page.calls)
            full = "_c1.innerHTML=''" in js
            # 从注入的 payload 里数 delete / items 数
            delet = 0
            items = 0
            idx = 0
            while True:
                j = js.find("window.patchBlocks('", idx)
                if j < 0:
                    break
                k = js.find(",", j)
                # 粗略：payload 是 JSON，找 '"delete": N'
                seg_end = js.find(");", k)
                seg = js[k:seg_end]
                import re

                m = re.search(r'"delete":\s*(\d+)', seg)
                if m:
                    delet += int(m.group(1))
                items += seg.count('"kind":')
                idx = seg_end
            events.append((tag, full, delet, items, len(js)))

        # 阶段 1：正文块 A 流式增长
        pos = 0
        while pos < len(text_a):
            pos = min(len(text_a), pos + CHUNK)
            blocks[1]["text"] = text_a[:pos]
            batch("textA")
        # 阶段 2：工具完成（fingerprints 变化）
        state["fps"] = frozenset({"tool-abc"})
        batch("toolDone")
        # 阶段 3：正文块 B 流式增长（新 text 块）
        pos = 0
        while pos < len(text_b):
            pos = min(len(text_b), pos + CHUNK)
            blocks[3]["text"] = text_b[:pos]
            batch("textB")
    finally:
        CodeWebViewer._LIVE_MIN_CHARS = old_min

    fulls = [e for e in events if e[1]]
    print(f"\n=== compact={compact} live={use_live} batches={len(events)} ===")
    print(f"force_full batches: {len(fulls)} -> {[e[0] for e in fulls]}")
    big = [e for e in events if e[2] > 1 or e[3] > 1]
    print(f"batches with multi-op (delete>1 or items>1): {len(big)}")
    for e in big[:8]:
        print(f"  {e[0]:8s} full={e[1]} delete={e[2]} items={e[3]} js={e[4]}")
    print(f"first batch: {events[0]}")
    print(f"tool-done batch: {[e for e in events if e[0] == 'toolDone']}")

def main():
    for compact in (True, False):
        for use_live in (True, False):
            run(compact, use_live)

if __name__ == "__main__":
    main()
