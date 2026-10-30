# -*- coding: utf-8 -*-
"""B5.2 流式纯文本尾态测试（活动块增量 append + 边界正式化）。

背景：v2 原先对活动块每批跑全量 markdown 并整块重传，30KB 回答实测累计
7.9~14.6MB 传输 / 4.5~5.1s 渲染（O(n²)）。B5.2 把活动块换成「纯文本节点 +
增量 append」，安全边界处整块正式化一次（节流 1s）。

覆盖：
1. render 层：live 块 key 恒定 → 内容增长不产生 patch op
2. 首帧全量纯文本落地（key = live:N）
3. 后续批次只发 ``_v2LiveAppend``，不重发 patchBlocks
4. 安全边界 + 节流满足 → 正式化（markdown 全量替换），之后回到 append
5. 文本回退/重写 → 整卡重落地（live 状态复位）
6. 短于门槛 → 不启用（保持原常规路径）
"""

from __future__ import annotations

import json as stdlib_json
import sys
from typing import Any, Dict, List, Optional

import pytest

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from app.widgets.card_viewers import CodeWebViewer
from app.widgets.render import BlockCache, RenderCtx, diff_message, live_key, render_message


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


class _StubPage:
    def __init__(self):
        self.js_calls: List[str] = []

    def runJavaScript(self, js_code, callback=None):
        self.js_calls.append(js_code)


class _FakeCard:
    def __init__(self, content_data: Any):
        self._content_data = content_data


def _make_viewer(monkeypatch, blocks: Any, streaming: bool = True):
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
    v._render_timer = QTimer()
    v._last_render_ts = 0.0
    v._markdown_text = "md"
    v._lazy_markdown_cb = None
    v._needs_full_render = True
    v._final_render_pending = False
    v._height_report_pending = False
    v._has_charts = False
    v._last_rendered_markdown = ""
    v._render_deferred = False
    v._page = _StubPage()
    monkeypatch.setattr(CodeWebViewer, "page", lambda self: self._page)
    monkeypatch.setattr(CodeWebViewer, "isVisible", lambda self: True)
    monkeypatch.setattr(CodeWebViewer, "parent", lambda self: _FakeCard(blocks))
    monkeypatch.setattr(CodeWebViewer, "_sync_streaming_dock", lambda self, active, collapse_after=False: None)
    monkeypatch.setattr(CodeWebViewer, "_v2_render_ctx", lambda self: RenderCtx(streaming=streaming))
    return v


def _patch_ops(js: str, container: str) -> List[Dict[str, Any]]:
    marker = f"window.patchBlocks('{container}',"
    out = []
    idx = 0
    while True:
        i = js.find(marker, idx)
        if i < 0:
            break
        start = i + len(marker)
        depth = 0
        for j in range(start, len(js)):
            if js[j] == "{":
                depth += 1
            elif js[j] == "}":
                depth -= 1
                if depth == 0:
                    out.append(stdlib_json.loads(js[start : j + 1]))
                    idx = j + 1
                    break
        else:
            break
    return out


def _long_text(n: int = 600) -> str:
    return "段落内容测试。" * (n // 7 + 1)


# ── 1. render 层：live key 恒定，内容增长不产生 patch ────────────────


def test_live_block_identity_stable_across_growth():
    ctx = RenderCtx(streaming=True)
    b1 = [{"type": "text", "text": "a" * 600}]
    b2 = [{"type": "text", "text": "a" * 900}]
    mr1 = render_message(b1, ctx, live_index=0, live_html="<div class='v2-live-text'>a</div>")
    mr2 = render_message(b2, ctx, live_index=0, live_html="<div class='v2-live-text'>ab</div>")
    assert mr1.content[0].key == live_key(0) == mr2.content[0].key
    c_ops, t_ops = diff_message(mr1, mr2)
    assert c_ops.is_empty and t_ops.is_empty, "活动块内容增长不得触发整块重传"


def test_live_block_skips_markdown():
    """live 块不跑 markdown：HTML 就是纯文本节点，不是 <p> 段落。"""
    ctx = RenderCtx(streaming=True)
    mr = render_message(
        [{"type": "text", "text": "# 标题\n\n正文"}], ctx, live_index=0, live_html="<div class='v2-live-text'>x</div>"
    )
    assert mr.content[0].html == "<div class='v2-live-text'>x</div>"
    assert "<h1" not in mr.content[0].html


def test_reasoning_active_block_uses_live_tail(monkeypatch):
    """思考流也走纯文本尾态：内容增长不产生 patch op，且归工具区（简洁模式）。"""
    content = "思考内容" * 200  # > _LIVE_MIN_CHARS
    blocks = [{"type": "reasoning", "content": content}]
    ctx_tool = RenderCtx(streaming=True, compact=True)

    mr1 = render_message(blocks, ctx_tool, live_index=0, live_html="<div class='v2-live-text think-compact'>x</div>")
    assert mr1.tool and not mr1.content, "reasoning live 块应归工具区（compact）"
    assert mr1.tool[0].key == live_key(0)

    blocks2 = [{"type": "reasoning", "content": content + "继续思考"}]
    mr2 = render_message(blocks2, ctx_tool, live_index=0, live_html="<div class='v2-live-text think-compact'>xy</div>")
    c_ops, t_ops = diff_message(mr1, mr2)
    assert c_ops.is_empty and t_ops.is_empty, "思考内容增长不得触发整块重传"


def test_reasoning_live_active_detection(monkeypatch):
    from app.widgets.card_viewers import CodeWebViewer as _V

    v = _make_viewer(monkeypatch, [])

    class _Blk(dict):
        pass

    blocks = [{"type": "reasoning", "content": "思考" * 400}]
    assert v._v2_live_active(blocks, 0) == 0, "reasoning 活动块应被 live 通道接受"

    short = [{"type": "reasoning", "content": "短"}]
    assert v._v2_live_active(short, 0) is None, "短内容仍走常规路径"


def test_finalize_incremental_skips_container_clear(monkeypatch):
    """流式结束的终渲染走增量：不清容器（免整卡重排，实测 450~490ms）。"""
    blocks = [{"type": "text", "text": _long_text()}]
    v = _make_viewer(monkeypatch, blocks, streaming=True)
    assert v._perform_update_v2() is True
    js1 = "".join(v._page.js_calls)
    assert "_c1.innerHTML=''" in js1, "首帧仍应清容器（全量落地）"

    v._page.js_calls.clear()
    v._streaming = False
    v._needs_full_render = True  # finish_streaming 会置位
    v._v2_finalize_incremental = True  # v2 终渲染增量标记
    blocks[0]["text"] = blocks[0]["text"] + "收尾追加的新段落内容。\n\n"
    monkeypatch.setattr(CodeWebViewer, "_v2_render_ctx", lambda self: RenderCtx(streaming=False))
    assert v._perform_update_v2() is True

    js2 = "".join(v._page.js_calls)
    assert "_c1.innerHTML=''" not in js2 and "_c2.innerHTML=''" not in js2, "终渲染应走增量 patch，不得清空容器"
    assert "window.patchBlocks(" in js2, "终渲染仍要下发块级补丁"


# ── 2. 首帧全量纯文本落地 ──────────────────────────────────────────


def test_first_frame_lands_plain_text(monkeypatch):
    text = _long_text()
    blocks = [{"type": "text", "text": text}]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True
    js = "".join(v._page.js_calls)
    ops = _patch_ops(js, "content-placeholder")
    assert len(ops) == 1
    item = ops[0]["items"][0]
    assert item["key"] == "live:0"
    assert "v2-live-text" in item["html"]
    assert "段落内容测试" in item["html"]
    assert v._v2_live_sent == text


# ── 3. 后续批次只发 append ────────────────────────────────────────


def test_growth_sends_append_only(monkeypatch):
    text = _long_text()
    blocks = [{"type": "text", "text": text}]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True

    v._page.js_calls.clear()
    v._needs_full_render = False
    delta = "追加的一小段。"
    blocks[0]["text"] = text + delta
    assert v._perform_update_v2() is True
    js = "".join(v._page.js_calls)
    assert "patchBlocks('content-placeholder'" not in js, "活动块续写不得重发整块"
    assert "_v2LiveAppend('content-placeholder',\"live:0\"," in js
    assert delta in js
    assert v._v2_live_sent == text + delta


# ── 4. 边界正式化 → 回到 append ───────────────────────────────────


def test_formalize_on_boundary_then_append_again(monkeypatch):
    text = _long_text()
    blocks = [{"type": "text", "text": text}]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True

    v._page.js_calls.clear()
    v._needs_full_render = False
    grown = text + "新增的正式段落。" * 40 + "\n\n"  # 新增 >256 且落在段落硬边界
    blocks[0]["text"] = grown
    assert v._perform_update_v2() is True
    js = "".join(v._page.js_calls)
    ops = _patch_ops(js, "content-placeholder")
    assert len(ops) == 1, "正式化应整块替换活动块"
    item = ops[0]["items"][0]
    assert item["key"] != "live:0", "正式化产物用 markdown 指纹 key"
    assert "<p>" in item["html"], "正式化必须走 markdown"
    # 落库基线保持恒定 key，避免下一批又判定替换
    assert v._v2_last.content[0].key == "live:0"

    # 正式化后继续追加：回到 append（不再整块重传）
    v._page.js_calls.clear()
    v._needs_full_render = False
    blocks[0]["text"] = grown + "继续流式。"
    assert v._perform_update_v2() is True
    js = "".join(v._page.js_calls)
    assert "patchBlocks('content-placeholder'" not in js
    assert "_v2LiveAppend(" in js


# ── 5. 文本回退 → 整卡重落地 ──────────────────────────────────────


def test_rollback_forces_full_reload(monkeypatch):
    text = _long_text()
    blocks = [{"type": "text", "text": text}]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True

    v._page.js_calls.clear()
    v._needs_full_render = False
    blocks[0]["text"] = "完全改写后的内容。" * 80  # 不以已下发文本为前缀
    assert v._perform_update_v2() is True
    js = "".join(v._page.js_calls)
    assert "_c1.innerHTML=''" in js, "文本回退必须整卡重落地"
    assert v._v2_live_sent == blocks[0]["text"]


# ── 6. 门槛以下不启用 ─────────────────────────────────────────────


def test_short_text_keeps_regular_path(monkeypatch):
    blocks = [{"type": "text", "text": "短文本"}]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True
    js = "".join(v._page.js_calls)
    ops = _patch_ops(js, "content-placeholder")
    assert ops and ops[0]["items"][0]["key"] != "live:0"
    assert v._v2_live_key == ""


# ── 7. 骨架 JS 防线：新增的两个函数必须真实存在且语法有效 ──────────


def test_tool_call_start_finalizes_live_tail(monkeypatch):
    """提问卡等阻塞工具：正文块在工具调用后不再增长，必须立即转正式 markdown。

    否则 ``_v2_live_should_formalize`` 等「新增 ≥256 字符」永远不满足，
    正文停在纯文本源码形态直到流式结束（用户报告：提问卡上方一块纯文本）。
    """
    text = "# 剩下的是物理清理\n\n" + "物理清理建议正文。" * 60
    blocks = [{"type": "text", "text": text}]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True
    assert v._v2_live_key == "live:0"
    assert "v2-live-text" in "".join(v._page.js_calls)

    v._page.js_calls.clear()
    v._needs_full_render = False
    v.mark_live_tail_finalized()  # 等价于工具调用开始
    js = "".join(v._page.js_calls)
    ops = _patch_ops(js, "content-placeholder")
    assert ops, "工具调用开始时必须把定型正文整块正式化"
    item = ops[0]["items"][0]
    assert item["key"] != "live:0", "定型块必须用 markdown 指纹 key"
    assert "<h1" in item["html"] and "<p>" in item["html"], "定型块必须走 markdown 转换"
    assert "v2-live-text" not in item["html"]
    assert v._v2_live_finalize_requested is False, "定型标记消费后必须清除"

def test_trailing_tool_block_means_text_finalized(monkeypatch):
    """工具结果块已落地：其前的 text 块不可能再增长，不得继续走 live 尾态。"""
    text = _long_text()
    blocks = [
        {"type": "text", "text": text},
        {
            "type": "tool_result",
            "name": "question",
            "tool_call_id": "t1",
            "result": '{"questions": []}',
            "success": True,
        },
    ]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True
    js = "".join(v._page.js_calls)
    ops = _patch_ops(js, "content-placeholder")
    assert ops, "末尾 text 块之后有工具块 → 必须正式化"
    keys = [it["key"] for it in ops[0]["items"]]
    assert all(k != "live:0" for k in keys)
    assert any("<p>" in it["html"] for it in ops[0]["items"])

def test_growing_text_after_tool_block_still_live(monkeypatch):
    """工具块之后的新 text 块仍在增长 → 正常走 live 尾态（不得误判定型）。"""
    blocks = [
        {"type": "text", "text": _long_text()},
        {"type": "tool_result", "name": "question", "tool_call_id": "t1", "result": "{}", "success": True},
        {"type": "text", "text": _long_text()},
    ]
    v = _make_viewer(monkeypatch, blocks)
    assert v._perform_update_v2() is True
    assert v._v2_live_key == live_key(2)

def _node_available() -> Optional[str]:
    import subprocess

    for cand in ("node", "node.exe"):
        try:
            subprocess.run([cand, "--version"], capture_output=True, check=True, timeout=5)
            return cand
        except Exception:
            continue
    return None


def _build_skeleton_html() -> str:
    """用桩 page 构建真实骨架 HTML（不拉 WebEngine）。"""
    _ensure_qapp()
    page = _StubPage()
    captured = {}
    v = CodeWebViewer.__new__(CodeWebViewer)
    v._light_skeleton = True
    v._streaming = False
    v._v2_last = None
    v.page = lambda: page
    v.setHtml = lambda html, url=None: captured.setdefault("html", html)
    v.ensure_transparent_composition = lambda: None
    v._load_skeleton()
    return captured.get("html", "")


@pytest.mark.skipif(_node_available() is None, reason="node 不可用")
def test_skeleton_scripts_compile(tmp_path):
    """骨架里每段 <script> 必须能通过 node 编译。

    runJavaScript / 内联脚本语法错误都是静默失败（无回调、无日志），
    stub 测试只能断言函数名存在，所以直接编译发出的真实脚本串。
    """
    import subprocess

    html = _build_skeleton_html()
    assert "window._v2LiveAppend" in html, "骨架缺 live append 通道"
    assert ".v2-live-text" in html, "骨架缺 live 样式"
    # 只认行首的真实标签：骨架 JS 内部有 ' + '<script>' + ' 这类字符串拼接，
    # 裸 split("<script>") 会在那里切错（把半截 JS 当脚本块）
    import re

    scripts = re.findall(r"(?m)^\s*<script>\s*$(.*?)^\s*</script>\s*$", html, re.S)
    assert scripts, "骨架里没有可编译的 script 块"
    for i, js in enumerate(scripts):
        harness = "try{new Function(" + stdlib_json.dumps(js) + ");console.log('OK');}"
        harness += "catch(e){console.log('ERR:'+e.message);process.exit(1);}"
        f = tmp_path / f"skel_{i}.js"
        f.write_text(harness, encoding="utf-8")
        r = subprocess.run([_node_available(), str(f)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"骨架脚本块 {i} 语法错误: {r.stdout[:300]} {r.stderr[:300]}"
        assert r.stdout.strip() == "OK", r.stdout
