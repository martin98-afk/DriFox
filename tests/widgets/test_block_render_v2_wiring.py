# -*- coding: utf-8 -*-
"""步骤 3 接线测试：CodeWebViewer 的 block 级渲染通道（DRIFOX_RENDER_V2=1）。

覆盖：
1. ``_v2_blocks`` 数据源解析（父链 ``_content_data``：块列表 / 字符串 / 缺失）
2. ``_perform_update_v2`` 首渲全量落地（清容器 + 两容器 patch payload）
3. 流式增量最小性：内容增长后只替换活动块，历史块不重发
4. 流式→结束切换：字符统计节点随 key 维度（":cc"）被替换移除
5. ctx 签名变化（主题 / compact 等）→ 缓存清空 + 全量重落地
6. ``_append_text_incremental`` 在 v2 下短路（不发 JS）
7. ``_perform_update`` 入口分支：v2 接管时旧路不执行

viewer 用 ``CodeWebViewer.__new__`` 桩（项目惯例，不拉 WebEngine），
page / parent / ctx 用 monkeypatch 注入。
"""

from __future__ import annotations

import json as stdlib_json
import sys
from typing import Any, Dict, List, Optional

import pytest

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from app.widgets.card_viewers import CodeWebViewer
from app.widgets.render import BlockCache, RenderCtx


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


class _StubPage:
    """记录 runJavaScript 调用（无 WebEngine）"""

    def __init__(self):
        self.js_calls: List[str] = []

    def runJavaScript(self, js_code, callback=None):
        self.js_calls.append(js_code)


class _FakeCard:
    """带 ``_content_data`` 的父级 MessageCard 桩"""

    def __init__(self, content_data: Any):
        self._content_data = content_data


class _NoContentCard:
    """无 ``_content_data`` 的祖先桩"""


def _ctx(streaming: bool = True, compact: bool = False) -> RenderCtx:
    return RenderCtx(streaming=streaming, compact=compact)


def _make_viewer(monkeypatch, blocks: Any, streaming: bool = True, ctx: Optional[RenderCtx] = None):
    """构造可调 ``_perform_update_v2`` 的 viewer 桩。"""
    _ensure_qapp()
    v = CodeWebViewer.__new__(CodeWebViewer)
    v._is_js_ready = True
    v._streaming = streaming
    v._light_skeleton = False
    v._dock_sync_requested = True  # 跳过坞态分支
    v._render_v2 = True
    v._block_cache = BlockCache()
    v._v2_last = None
    v._v2_ctx_sig = None
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
    monkeypatch.setattr(CodeWebViewer, "_v2_render_ctx", lambda self: ctx or _ctx(streaming))
    return v


def _patch_ops(js: str, container: str) -> List[Dict[str, Any]]:
    """从发出的 JS 里抽 patchBlocks 调用的 ops（测试断言用）。"""
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


# ── 1. 数据源解析 ────────────────────────────────────────────────────


def test_v2_blocks_from_parent_chain(monkeypatch):
    v = _make_viewer(monkeypatch, [{"type": "text", "text": "hi"}])
    assert v._v2_blocks() == [{"type": "text", "text": "hi"}]


def test_v2_blocks_none_for_string_content(monkeypatch):
    """用户 / 欢迎消息的内容是 str → 无块源，回退旧路径。"""
    v = _make_viewer(monkeypatch, "plain markdown")
    assert v._v2_blocks() is None


def test_v2_blocks_none_without_parent_data(monkeypatch):
    v = _make_viewer(monkeypatch, [{"type": "text", "text": "hi"}])
    monkeypatch.setattr(CodeWebViewer, "parent", lambda self: _NoContentCard())
    assert v._v2_blocks() is None


# ── 2. 首渲全量落地 ─────────────────────────────────────────────────


def test_first_render_full_reset_and_two_containers(monkeypatch):
    blocks = [
        {"type": "text", "text": "正文"},
        {"type": "tool_result", "tool_call_id": "t1", "name": "read", "result": "r", "success": True},
    ]
    # compact=True：工具块 home=tool → 验证双容器分流
    v = _make_viewer(monkeypatch, blocks, streaming=False, ctx=_ctx(False, compact=True))
    assert v._perform_update_v2() is True
    js = "".join(v._page.js_calls)
    # 全量落地必须先清两个容器（旧路径节点不被 splice 认领）
    assert "_c1.innerHTML=''" in js and "_c2.innerHTML=''" in js
    content_ops = _patch_ops(js, "content-placeholder")
    tool_ops = _patch_ops(js, "tool-content")
    assert len(content_ops) == 1 and content_ops[0]["base"] == 0
    assert len(tool_ops) == 1
    # 收尾通道必须被调用
    assert "window._v2AfterPatch(false)" in js


def test_streaming_char_count_node_present_then_removed(monkeypatch):
    """流式渲染末块带字符统计节点（key 带 ":cc"），结束后被替换移除。"""
    blocks = [{"type": "text", "text": "hello"}]
    v = _make_viewer(monkeypatch, blocks, streaming=True)
    assert v._perform_update_v2() is True
    js = "".join(v._page.js_calls)
    # payload 走 JSON 转义，引号变 \" ；此处只断言节点 id 存在
    assert "char-count" in js

    # 流式期间内容增长：活动块被替换（非全量），统计节点仍在
    v._page.js_calls.clear()
    blocks[0]["text"] = "hello world"
    v._needs_full_render = False
    assert v._perform_update_v2() is True
    js = "".join(v._page.js_calls)
    assert "_c1.innerHTML=''" not in js, "非全量帧不得清容器"
    ops = _patch_ops(js, "content-placeholder")
    assert len(ops) == 1
    assert ops[0]["base"] == 0 and ops[0]["delete"] == 1 and len(ops[0]["items"]) == 1
    assert 'id="char-count"' in ops[0]["items"][0]["html"]

    # 流式结束：key 从 ":cc" 变回 → 全帧替换后统计节点消失
    v._page.js_calls.clear()
    v._streaming = False
    monkeypatch.setattr(CodeWebViewer, "_v2_render_ctx", lambda self: _ctx(False))
    v._needs_full_render = True
    assert v._perform_update_v2() is True
    js = "".join(v._page.js_calls)
    ops = _patch_ops(js, "content-placeholder")
    assert all('id="char-count"' not in it["html"] for op in ops for it in op["items"])


def test_unchanged_content_emits_no_patch(monkeypatch):
    blocks = [{"type": "text", "text": "same"}]
    v = _make_viewer(monkeypatch, blocks, streaming=False)
    assert v._perform_update_v2() is True
    n = len(v._page.js_calls)
    v._needs_full_render = False
    v._final_render_pending = False
    assert v._perform_update_v2() is True
    assert len(v._page.js_calls) == n, "内容未变不得重发补丁"


# ── 3. ctx 签名变化 → 全量重落地 ────────────────────────────────────


def test_ctx_sig_change_forces_full_reapply(monkeypatch):
    blocks = [{"type": "text", "text": "hi"}]
    v = _make_viewer(monkeypatch, blocks, streaming=False, ctx=_ctx(False, compact=False))
    assert v._perform_update_v2() is True
    assert v._v2_last is not None

    v._page.js_calls.clear()
    v._needs_full_render = False
    monkeypatch.setattr(CodeWebViewer, "_v2_render_ctx", lambda self: _ctx(False, compact=True))
    assert v._perform_update_v2() is True
    js = "".join(v._page.js_calls)
    assert "_c1.innerHTML=''" in js, "ctx 变化必须全量重落地"
    assert v._block_cache.stats["size"] == 0 or v._block_cache.stats["misses"] > 0


# ── 4. v2 短路与入口分支 ────────────────────────────────────────────


def test_append_text_incremental_noop_in_v2(monkeypatch):
    v = _make_viewer(monkeypatch, [{"type": "text", "text": "hi"}])
    n = len(v._page.js_calls)
    v._append_text_incremental("more")
    assert len(v._page.js_calls) == n, "v2 下不得注入纯文本增量节点"


def test_perform_update_v2_takes_over_and_skips_legacy(monkeypatch):
    """_perform_update 入口：v2 接管后旧路径（线程池渲染）不得执行。"""
    v = _make_viewer(monkeypatch, [{"type": "text", "text": "hi"}], streaming=False)
    monkeypatch.setattr(CodeWebViewer, "_refresh_viewer_font_css", lambda self: None)
    monkeypatch.setattr(CodeWebViewer, "_height_report_pending", False, raising=False)

    def _boom(self, md, compact):
        raise AssertionError("v2 接管后不得走旧路径线程池渲染")

    monkeypatch.setattr(CodeWebViewer, "_sequence_render", _boom)
    monkeypatch.setattr(CodeWebViewer, "_render_markdown_to_html", _boom)
    v._perform_update()
    assert any("patchBlocks" in js for js in v._page.js_calls)


def test_perform_update_v2_reports_fallback_without_blocks(monkeypatch):
    """无块源（str 内容）：``_perform_update_v2`` 返回 False，调用方负责关开关。"""
    v = _make_viewer(monkeypatch, "plain", streaming=False)
    assert v._perform_update_v2() is False
    assert v._v2_last is None


def test_no_blocks_keeps_v2_disabled(monkeypatch):
    """回退后再次渲染不再尝试 v2（防每帧白试）。"""
    v = _make_viewer(monkeypatch, "plain", streaming=False)
    monkeypatch.setattr(CodeWebViewer, "_refresh_viewer_font_css", lambda self: None)
    monkeypatch.setattr(CodeWebViewer, "_sequence_render", lambda self, md, compact: None)
    v._perform_update()
    assert v._render_v2 is False

# ── 回归防线：全量落地串必须语法有效 ─────────────────────────────────

def _node_available() -> Optional[str]:
    import subprocess

    for cand in ("node", "node.exe"):
        try:
            subprocess.run([cand, "--version"], capture_output=True, check=True, timeout=5)
            return cand
        except Exception:
            continue
    return None

@pytest.mark.skipif(_node_available() is None, reason="node 不可用")
def test_full_landing_js_compiles(monkeypatch, tmp_path):
    """首渲（force_full）发出的整串 JS 必须能通过语法编译。

    回归背景（2026-10-29）：清容器段 ``clear();}}`` 多一个 ``}`` 把 try 提前
    闭合，整串 SyntaxError → runJavaScript 静默丢弃 → 历史卡全量落地永久
    失败（整卡空白、零日志）。stub page 只记录不执行，语法错误无任何症状，
    故用 node ``new Function`` 编译发出的真实 JS 串。
    """
    import subprocess

    v = _make_viewer(
        monkeypatch,
        [{"type": "text", "text": "# 标题\n\n正文段落，含**加粗**与 `code`。"}],
        streaming=False,
    )
    assert v._perform_update_v2() is True
    full_js = [c for c in v._page.js_calls if "patchBlocks" in c]
    assert full_js, "首渲必须发出 patchBlocks 全量落地串"
    js = full_js[0]
    assert "content-placeholder" in js and "_v2AfterPatch" in js

    harness = "try{new Function(" + stdlib_json.dumps(js) + ");console.log('OK');}"
    harness += "catch(e){console.log('ERR:'+e.message);process.exit(1);}"
    f = tmp_path / "compile_check.js"
    f.write_text(harness, encoding="utf-8")
    r = subprocess.run(
        [_node_available(), str(f)], capture_output=True, text=True, timeout=30
    )
    assert r.returncode == 0, f"全量落地串语法错误: {r.stdout} {r.stderr[:400]}"
    assert r.stdout.strip() == "OK", r.stdout
