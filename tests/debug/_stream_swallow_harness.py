# -*- coding: utf-8 -*-
"""临时诊断 harness：无头复现「流式过程中正文/工具框吞内容」。

做法（diagnose Phase 1 反馈循环）：
- 用桩 viewer 绑定 CodeWebViewer 的真实渲染方法（_perform_update /
  _render_markdown_to_html / _apply_render_result ...），page() 返回假页。
- 假页把 runJavaScript 的 updateContent / updateContentAppend /
  updateTailHtml 按 JS 语义作用到一个极简 DOM 模型（节点列表）。
- MessageCard 走真实 append_text 链路（含 _append_text_incremental 的
  DOM 增量写入）。
- 每个 chunk 注入唯一 token《N》，每步断言 DOM 可见文本包含全部已发 token。
  缺失即「吞内容」，打印首次缺失时的完整状态。

运行：python tests/debug/_stream_swallow_harness.py
"""

import json
import os
import re
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)

from app.widgets import message_card as mc  # noqa: E402
from app.widgets.message_card import CodeWebViewer, MessageCard  # noqa: E402

# 桩对象不是 QObject，屏蔽 sip 存活检测
mc.sip = types.SimpleNamespace(isdeleted=lambda o: False)


_VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "source"}


def _split_top_blocks(html: str):
    """按顶层块级元素切分 HTML（手写栈扫描，够用即可）。

    updateContent 的 html 会被 _mark_unclosed_para_js 针对**末尾 <p>** 打标，
    模型必须能把该 <p> 单独摘出来，否则会误报重复渲染。
    """
    blocks = []
    i = 0
    n = len(html)
    buf = ""
    stack = []
    while i < n:
        if html[i] == "<":
            m = re.match(r"</?([a-zA-Z0-9]+)", html[i:])
            if not m:
                buf += html[i]
                i += 1
                continue
            tag = m.group(1).lower()
            closing = html[i + 1] == "/"
            end = html.find(">", i)
            if end == -1:
                buf += html[i:]
                break
            if closing:
                buf += html[i : end + 1]
                if stack and stack[-1] == tag:
                    stack.pop()
                    if not stack:
                        blocks.append(buf)
                        buf = ""
                i = end + 1
                continue
            buf += html[i : end + 1]
            self_closing = html[end - 1] == "/"
            if not self_closing and tag not in _VOID_TAGS:
                stack.append(tag)
            i = end + 1
            continue
        buf += html[i]
        i += 1
    if buf:
        blocks.append(buf)
    return [b for b in blocks if b.strip()]


class Dom:
    """极简 DOM 模型：节点列表 (kind, text, incremental, rendered)。"""

    def __init__(self):
        self.nodes = []

    def _set_last_para_incremental(self):
        """模拟 _mark_unclosed_para_js：末尾块级元素打 data-incremental。

        与实现同步：跳过 char-count / 图表容器；覆盖 P/PRE/UL/OL/BLOCKQUOTE/
        H1-6 与「含 <pre> 的 DIV」（代码块包装）。
        """
        for i in range(len(self.nodes) - 1, -1, -1):
            kind, s, inc, rendered = self.nodes[i]
            if kind != "html":
                continue
            if 'id="char-count"' in s or "id='char-count'" in s:
                continue  # JS: _l.id==='char-count' → 取前一个兄弟
            if any(x in s for x in ("echarts-container", "mermaid-block", "katex-container")):
                return
            m = re.match(r"<([a-zA-Z0-9]+)", s.lstrip().lower())
            tag = m.group(1) if m else ""
            is_pre = tag == "pre" or (tag == "div" and "<pre" in s.lower())
            if tag in ("p", "pre", "ul", "ol", "blockquote") or re.match(r"^h[1-6]$", tag) or is_pre:
                self.nodes[i] = (kind, s, True, False)
                return
            return

    def apply_js(self, js: str):
        dec = json.JSONDecoder()
        if js.startswith("updateContentAppend("):
            rest = js[len("updateContentAppend(") :]
            new_html, idx = dec.raw_decode(rest)
            rest2 = rest[idx:].lstrip()
            tail_html, _ = dec.raw_decode(rest2[1:])
            self.nodes = [n for n in self.nodes if not n[2]]
            self.nodes.extend(("html", b, False, False) for b in _split_top_blocks(new_html))
            if tail_html:
                self.nodes.append(("html", tail_html, True, True))
            return
        if js.startswith("updateTailHtml("):
            rest = js[len("updateTailHtml(") :]
            html, _ = dec.raw_decode(rest)
            self.nodes = [n for n in self.nodes if not n[2]]
            if html:
                self.nodes.append(("html", html, True, True))
            return
        if js.startswith("updateContent("):
            rest = js[len("updateContent(") :]
            html, _ = dec.raw_decode(rest)
            self.nodes = [("html", b, False, False) for b in _split_top_blocks(html)]
            if "setAttribute('data-incremental','true')" in js:
                self._set_last_para_incremental()
            return

    def append_text(self, text: str):
        """模拟 _dfxAppendStreamText：末尾增量节点是已渲染 tail → 另起纯文本节点。"""
        for i in range(len(self.nodes) - 1, -1, -1):
            kind, s, inc, rendered = self.nodes[i]
            if inc:
                if rendered:
                    self.nodes.append(("text", text, True, False))
                else:
                    self.nodes[i] = ("text", s + text, True, False)
                return
        self.nodes.append(("text", text, True, False))

    def visible_text(self) -> str:
        parts = []
        for kind, s, *_ in self.nodes:
            parts.append(re.sub(r"<[^>]+>", "", s) if kind == "html" else s)
        return "".join(parts)


class FakePage:
    def __init__(self, dom):
        self.dom = dom
        self.calls = []

    def runJavaScript(self, js, cb=None):
        self.calls.append(js[:80])
        self.dom.apply_js(js)
        if cb is not None:
            cb(None)


class ViewerStub:
    """绑定 CodeWebViewer 真实渲染逻辑的桩（无 Chromium）。"""

    # ── 真实方法 ──
    _perform_update = CodeWebViewer._perform_update
    _render_markdown_to_html = CodeWebViewer._render_markdown_to_html
    _apply_render_result = CodeWebViewer._apply_render_result
    _has_active_tool_dom = CodeWebViewer._has_active_tool_dom
    _render_tail_inline = CodeWebViewer._render_tail_inline
    _clear_tool_dom_dirty_guarded = CodeWebViewer._clear_tool_dom_dirty_guarded
    _push_unrendered_tail_text = CodeWebViewer._push_unrendered_tail_text
    _append_text_incremental_real = CodeWebViewer._append_text_incremental
    _has_reached_clean_boundary = staticmethod(CodeWebViewer._has_reached_clean_boundary)
    _has_reached_soft_boundary = staticmethod(CodeWebViewer._has_reached_soft_boundary)

    # ── 常量/开关（避开依赖 UI 设置的 property）──
    _ASYNC_HISTORY_RENDER_MIN_CHARS = CodeWebViewer._ASYNC_HISTORY_RENDER_MIN_CHARS
    _CHAR_COUNT_HTML = mc._CHAR_COUNT_HTML
    _tool_compact_mode = False
    _tool_target_id = "tool-content"

    def __init__(self):
        self.dom = Dom()
        self._page = FakePage(self.dom)
        self._markdown_text = ""
        self._streaming = True
        self._is_history = False
        self._is_js_ready = True
        self._final_render_pending = False
        self._last_rendered_html = ""
        self._last_rendered_markdown = ""
        self._processed_md_hash = None
        self._cached_streaming_html = None
        self._cached_raw_md_hash = 0
        self._lazy_markdown_cb = None
        self._tool_md_cache = {}
        self._tool_dom_dirty = False
        self._tool_dom_dirty_gen = 0
        self._injected_pending_tools = set()
        self._render_seq = 0
        self._render_inflight = False
        self._render_pending = None
        self._render_deferred = False
        self._theme_css_pending = False
        self._stable_html = ""
        self._stable_md_len = 0
        self._tail_html_hash = 0
        self._needs_full_render = True
        self._light_skeleton = False
        self._min_render_interval = 80
        self._height_report_pending = False
        self._thinking_finalized = False
        self._reasoning_streaming_started = False
        self._think_text_streaming_started = False
        self._last_chunk_time = 0.0
        self._current_adaptive_interval = 300
        self._tool_target_id_override = "tool-content"
        self._restore_finished_ids = set()
        self._finish_t0 = 0.0
        self._timer_pending = False  # harness 用：是否有待触发的安全定时器
        self._inflight_task = None  # harness 用：在途线程池任务 (seq, md)

    # ── 桩替换 ──
    def page(self):
        return self._page

    def parent(self):
        return None

    def isVisible(self):
        return True

    def _refresh_viewer_font_css(self):
        pass

    def _build_save_and_restore_js(self, html_content, finished_ids=None):
        return f"updateContent({json.dumps(html_content)});"

    def _append_text_incremental(self, text: str):
        self.dom.append_text(text)

    def _schedule_render(self, immediate: bool = False):
        if immediate:
            self._perform_update()
        else:
            self._timer_pending = True

    async_mode = False  # True=模拟线程池在途延迟（结果延迟一拍才落地）

    def _sequence_render(self, md: str, compact: bool):
        """线程池渲染：async_mode 下延迟一拍应用（复现 seq 过期/pending 续派）。

        注意：不修改 _last_rendered_markdown（与真实代码一致——真实代码在
        调用点 9502 设置，9474 分支未设置，此处保持忠实）。
        """
        self._render_seq += 1
        seq = self._render_seq
        if not self.async_mode:
            self._render_inflight = True
            self._apply_render_result(seq, self._render_markdown_to_html(md))
            return
        if self._render_inflight:
            self._render_pending = (seq, md, compact)
            return
        self._render_inflight = True
        self._inflight_task = (seq, md)

    def flush_async(self):
        """模拟线程池任务完成回调（主线程应用结果 + 续派 pending）。"""
        if not getattr(self, "_inflight_task", None):
            return
        seq, md = self._inflight_task
        self._inflight_task = None
        try:
            self._apply_render_result(seq, self._render_markdown_to_html(md))
        finally:
            pass

    def flush_timer(self):
        if self._timer_pending:
            self._timer_pending = False
            self._perform_update()


def make_card():
    card = MessageCard(role="assistant")
    card._lazy_rendered = True
    card.viewer = ViewerStub()
    card._streaming = True
    card._content_data = []
    return card


def run_case(name, chunks, flush_every=1, mode="sync", seed=0, verbose=False, ignore=()):
    """ignore：不参与断言的 token。

    两类合法"变形"不算吞内容：
    - `<tool>` 块内的 token：闭合后被渲染成结构化工具框（args 表格），
      原文形态本就不复存在；
    - 思考块 token：think 卡片的 summary 预览 + body 各含一份文本，
      dup 计数会天然翻倍。
    """
    """跑一条流式序列，返回失败详情（None=通过）。

    mode:
      sync  —— _sequence_render 立即应用（确定性）
      async —— 提交后延迟一拍应用，按 seed 随机穿插 flush（复现竞态）
    """
    import random

    rng = random.Random(seed)
    card = make_card()
    viewer = card.viewer
    viewer.async_mode = mode == "async"
    sent = []
    worst_transient = 0
    for i, chunk in enumerate(chunks):
        card.append_text(chunk)
        sent.append(chunk)
        if viewer.async_mode:
            if rng.random() < 0.5:
                viewer.flush_async()
            if rng.random() < 0.3:
                viewer.flush_timer()
        elif flush_every and (i + 1) % flush_every == 0:
            viewer.flush_timer()
        missing_now = [t for t in _tokens(sent) if t not in ignore and t not in viewer.dom.visible_text()]
        worst_transient = max(worst_transient, len(missing_now))
        if verbose:
            print(
                f"  step{i} chunk={chunk!r} stable={viewer._stable_md_len} "
                f"missing={missing_now} inc_flags={[n[2] for n in viewer.dom.nodes]}"
            )
            print(f"        md={viewer._markdown_text!r}")
            print(f"        vis={viewer.dom.visible_text()!r}")
    # 收敛：把在途任务与待触发定时器全部跑完
    for _ in range(8):
        viewer.flush_async()
        viewer.flush_timer()
    visible = viewer.dom.visible_text()
    checked = [t for t in _tokens(sent) if t not in ignore]
    missing = [t for t in checked if t not in visible]
    dup = _dup_tokens(visible, checked)
    if missing or dup:
        print(f"[FAIL] {name}: missing={missing[:6]} dup={dup[:6]} transient={worst_transient}")
        print(f"  md={viewer._markdown_text!r}")
        print(f"  visible={visible!r}")
        return {"missing": missing, "dup": dup, "visible": visible}
    print(f"[OK] {name} (瞬时最大缺 token={worst_transient})")
    return None


_TOKEN_RE = re.compile(r"《[^》]*》")


def _tokens(chunks):
    """提取所有注入 token（chunk 内嵌也要算，不能只认整 chunk 为 token）。"""
    out = []
    for c in chunks:
        out.extend(_TOKEN_RE.findall(c))
    return out


def _dup_tokens(visible, tokens):
    """同一 token 在可见文本中出现 2 次以上 = 重复渲染（content 错乱）。"""
    return [t for t in tokens if visible.count(t) > 1]


# ── 用例构造 ──
THINK_IGNORE = tuple(f"《think{i}》" for i in range(64))
TOOL_IGNORE = ("《1》", "《2》")


def sentence_chunks(n=25):
    """普通中文正文：句号软边界 + 段落空行。"""
    out = []
    for i in range(n):
        out.append(f"《{i}》")
        out.append("这是一段用于验证流式渲染完整性的中文句子")
        if i % 4 == 3:
            out.append("。\n\n")
        elif i % 2 == 1:
            out.append("。")
        else:
            out.append(" 继续")
    return out


def long_para_chunks(n=25):
    """无空行长段落（只在末尾一个 \n\n）。"""
    out = []
    for i in range(n):
        out.append(f"《{i}》长段落里的第{i}句话")
    out.append("。\n\n")
    return out


def code_chunks(n=12):
    out = ["《0》下面是代码示例\n\n```python\n"]
    for i in range(1, n):
        out.append(f"《{i}》line_{i} = {i}\n")
    out.append("```\n\n《end》结束说明。")
    return out


def think_chunks(n=12):
    out = ["<think>《think0》开始思考"]
    for i in range(1, n):
        out.append(f"《think{i}》思考第{i}步")
    out.append("</think>\n\n《body》正文开始，这里是最终回答。")
    return out


def unclosed_tool_chunks():
    """正文 + 未闭合 <tool> 流式（工具调用参数边生成边显示）。"""
    return [
        "《0》准备调用工具。\n\n",
        "《pre》以下是调用参数说明",
        '<tool>《1》{"name": "read_file",',
        '《2》"args": {"path": "a.txt"}}',
        "</tool>\n\n",
        "《3》工具结果说明。",
    ]


def mixed_chunks():
    """正文 + 代码块 + 思考混合。"""
    out = ["《0》开头说明。\n\n", "```python\n《1》x = 1\n", "《2》y = 2\n", "```\n\n"]
    out.append("<think>《think20》思考中")
    out.append("《think21》继续思考")
    out.append("</think>\n\n")
    out.append("《5》结论段落一。\n\n")
    out.append("《6》结论段落二")
    return out


if __name__ == "__main__":
    if "--verbose" in sys.argv:
        run_case("思考块(verbose)", think_chunks(), 1, verbose=True)
        run_case("未闭合tool尾部(verbose)", unclosed_tool_chunks(), 1, verbose=True)
        raise SystemExit(0)
    cases = []
    for name, chunks, every in [
        ("普通正文-每chunk刷新", sentence_chunks(), 1),
        ("普通正文-每3chunk刷新", sentence_chunks(), 3),
        ("长段落无空行", long_para_chunks(), 1),
        ("长段落每3刷新", long_para_chunks(), 3),
        ("代码块", code_chunks(), 1),
        ("思考块", think_chunks(), 1),
        ("未闭合tool尾部", unclosed_tool_chunks(), 1),
        ("混合内容", mixed_chunks(), 1),
    ]:
        ignore = THINK_IGNORE if "思考" in name else (TOOL_IGNORE if "tool" in name else ())
        if "混合" in name:
            ignore = THINK_IGNORE
        cases.append((name, chunks, every, "sync", 0, ignore))
    # 异步竞态：多种子 fuzz
    for seed in range(6):
        cases.append((f"异步fuzz-正文-seed{seed}", sentence_chunks(), 1, "async", seed, ()))
        cases.append((f"异步fuzz-长段-seed{seed}", long_para_chunks(), 1, "async", seed, ()))
        cases.append((f"异步fuzz-代码-seed{seed}", code_chunks(), 1, "async", seed, ()))
        cases.append((f"异步fuzz-思考-seed{seed}", think_chunks(), 1, "async", seed, THINK_IGNORE))
        cases.append((f"异步fuzz-tool-seed{seed}", unclosed_tool_chunks(), 1, "async", seed, TOOL_IGNORE))
        cases.append((f"异步fuzz-混合-seed{seed}", mixed_chunks(), 1, "async", seed, THINK_IGNORE))
    failures = 0
    for name, chunks, every, mode, seed, ignore in cases:
        if run_case(name, chunks, flush_every=every, mode=mode, seed=seed, ignore=ignore):
            failures += 1
    print(f"\n失败用例数: {failures}/{len(cases)}")
