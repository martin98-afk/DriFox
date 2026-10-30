# -*- coding: utf-8 -*-
"""v2 单帧渲染成本分解基准（Python 侧，不含 WebEngine 绘制）。

目的：优化前先量化。把一次 ``_perform_update_v2`` 拆成可归因的阶段，
定位流式期间每批的真实开销；WebEngine 侧的 JS 执行/布局不在本基准内。

阶段：
    markdown_cb    _lazy_markdown_cb() → content_to_markdown（每批 O(n) 重建）
    render         render_message（BlockCache 命中情况下的剩余成本）
    diff           diff_message（块级 diff）
    json           ops → JSON 串（IPC 载荷构建）
    has_charts     _mark_has_charts（join 全部块 html 后扫描）
    total          _perform_update_v2 总耗时

场景：30KB 正文 + 3 个思考块 + 5 个工具块，流式 300 批（每批 +120 字符）。

运行：python tests/debug/_bench_v2_frame_cost.py
"""

import json as stdlib_json
import os
import statistics
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("DRIFOX_DATA_DIR", str(_ROOT / ".drifox-test"))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from app.widgets import card_viewers  # noqa: E402
from app.widgets.card_viewers import CodeWebViewer  # noqa: E402
from app.widgets.render import BlockCache, RenderCtx  # noqa: E402


class _StubPage:
    def __init__(self):
        self.js_calls = []
        self.js_bytes = 0

    def runJavaScript(self, js_code, callback=None):
        self.js_calls.append(js_code)
        self.js_bytes += len(js_code)


class _FakeCard:
    def __init__(self, content):
        self._content_data = content


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


def _make_viewer(blocks, streaming=True, compact=True):
    _ensure_qapp()
    v = CodeWebViewer.__new__(CodeWebViewer)
    v._is_js_ready = True
    v._streaming = streaming
    v._light_skeleton = False
    v._dock_sync_requested = True
    v._render_v2 = True
    v._block_cache = BlockCache()
    v._v2_last = None
    v._v2_ctx_sig = None
    v._v2_live_key = ""
    v._v2_live_sent = ""
    v._v2_live_formalized_len = 0
    v._v2_live_formalize_ts = 0.0
    v._v2_live_finalize_requested = False
    v._markdown_text = ""
    v._lazy_markdown_cb = None
    v._needs_full_render = True
    v._final_render_pending = False
    v._height_report_pending = False
    v._has_charts = False
    v._last_rendered_markdown = ""
    v._render_deferred = False
    v._page = _StubPage()
    v._v2_effective_compact = compact
    CodeWebViewer.page = lambda self: self._page
    CodeWebViewer.isVisible = lambda self: True
    CodeWebViewer.parent = lambda self: _FakeCard(blocks)
    CodeWebViewer._sync_streaming_dock = lambda self, active, collapse_after=False: None
    CodeWebViewer._v2_render_ctx = lambda self: RenderCtx(streaming=streaming, compact=compact)
    return v


def _build_blocks(n_chars: int):
    para = "这是一段用于基准测试的正文内容，包含中文与 `code` 与 **加粗**，用来模拟真实流式输出。\n\n"
    text = para * max(1, n_chars // len(para))
    return [
        {"type": "reasoning", "content": "思考内容" * 40},
        {"type": "tool_result", "tool_call_id": "t1", "name": "bash", "arguments": {"command": "ls"}, "content": "x" * 400, "success": True},
        {"type": "reasoning", "content": "第二段思考" * 40},
        {"type": "tool_result", "tool_call_id": "t2", "name": "read", "arguments": {"filePath": "a.py"}, "content": "y" * 400, "success": True},
        {"type": "text", "text": text},
    ]


def main() -> None:
    from app.core.conversation.message_content import content_to_markdown

    n_chars = int(os.environ.get("BENCH_CHARS", "30000"))
    n_batches = int(os.environ.get("BENCH_BATCHES", "300"))
    step = max(50, n_chars // n_batches)
    formalize_each = os.environ.get("BENCH_FORMALIZE", "1") == "1"

    blocks = _build_blocks(step)
    viewer = _make_viewer(blocks)

    acc = 0
    samples = {"markdown_cb": [], "total": [], "json_bytes": [], "ops_items": []}
    js_bytes_total = 0

    def _run():
        nonlocal acc, js_bytes_total
        for i in range(n_batches):
            head = blocks[-1]["text"]
            blocks[-1]["text"] = head + ("补" * step)
            acc += step

            t0 = time.perf_counter()
            viewer._markdown_text = content_to_markdown(blocks)
            t1 = time.perf_counter()
            viewer._needs_full_render = False
            viewer._v2_live_finalize_requested = formalize_each
            before_js = len(viewer._page.js_calls)
            viewer._perform_update_v2()
            t2 = time.perf_counter()

            samples["markdown_cb"].append((t1 - t0) * 1000)
            samples["total"].append((t2 - t1) * 1000)
            new_js = viewer._page.js_calls[before_js:]
            js_bytes_total += sum(len(j) for j in new_js)
            for j in new_js:
                if "patchBlocks('tool-content'" in j or "patchBlocks('content-placeholder'" in j:
                    samples["json_bytes"].append(len(j))

    if os.environ.get("BENCH_PROFILE") == "1":
        import cProfile
        import io
        import pstats

        pr = cProfile.Profile()
        pr.enable()
        _run()
        pr.disable()
        buf = io.StringIO()
        pstats.Stats(pr, stream=buf).sort_stats("tottime").print_stats(18)
        text = buf.getvalue()
        # 只留函数行，去掉表头噪声
        for line in text.splitlines():
            s = line.strip()
            if s and (s[0].isdigit() or "cumtime" in s):
                print(s[:150])
    else:
        t_start = time.perf_counter()
        _run()
        wall = (time.perf_counter() - t_start) * 1000
        print(f"场景: {acc} 字符 / {n_batches} 批 / 每批 +{step} 字符 / 每批正式化={formalize_each}")
        print(f"墙钟总耗时: {wall:.0f}ms（均 {wall / n_batches:.2f}ms/批）")
        print(f"  累计 IPC 载荷: {js_bytes_total / 1024:.0f}KB（均 {js_bytes_total / n_batches / 1024:.1f}KB/批）")
        return

    wall = 0.0
    print(f"场景: {acc} 字符 / {n_batches} 批 / 每批 +{step} 字符 / 每批正式化={formalize_each}")
    print(f"累计 IPC 载荷: {js_bytes_total / 1024:.0f}KB（均 {js_bytes_total / n_batches / 1024:.1f}KB/批）")

    def stat(key):
        arr = samples[key]
        if not arr:
            return "n/a"
        arr_sorted = sorted(arr)
        p = lambda q: arr_sorted[min(len(arr_sorted) - 1, int(len(arr_sorted) * q))]
        return (
            f"p50={statistics.median(arr):.2f} p90={p(0.9):.2f} p99={p(0.99):.2f} "
            f"max={max(arr):.2f} sum={sum(arr):.0f}"
        )

    print(f"  markdown_cb(ms): {stat('markdown_cb')}")
    print(f"  frame_total(ms): {stat('total')}")
    print(f"  js_payload(len): {stat('json_bytes')}")


if __name__ == "__main__":
    main()
