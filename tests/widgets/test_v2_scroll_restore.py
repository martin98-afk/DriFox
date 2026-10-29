# -*- coding: utf-8 -*-
"""回归测试：v2 渲染通道的滚动位置保全与折叠态稳定身份。

背景（用户实测，2026-10-30）
--------------------------
1. 流式输出时用户把卡内滚轮上滚到阅读位置，下一批更新自动跳回顶部。
   v1 的 ``updateContent`` 一直有 body / content-placeholder / tool-content
   三处 save+restore；v2（``_perform_update_v2``）接管 DOM 后整条链跳过。
   而 ``force_full`` 分支要 ``innerHTML=''`` 整页清空 → scrollHeight 归零 →
   浏览器把 scrollTop 钳到 0，重新灌入后不会自己回来。
   修复：清空前 ``_v2ScrollSave()``，patch 后 ``_v2ScrollRestore()``。

2. 流式中已展开的折叠框会重放一次展开动画。
   两层原因：
   a) 折叠态 Map 以 ``data-block-key``（思考块是**内容 sha1**）为索引，
      内容一变 key 就变 → 节点重建后失配 → 回落到 HTML 写死的折叠态。
      修复：``_collapseKeyOf`` 优先用 ``data-flip-key`` / ``data-tool-call-id``。
   b) patchBlocks 重建节点后 ``restoreCollapsibleStates`` 把 data-expanded
      改回 true，chevron 旋转 / border-color 的 220ms 过渡从默认态重播。
      修复：插入前冻结 transition（``_fz``，与 updateContent 同机制）。

测试分两类：Python 侧断言下发 JS 的顺序，源码级断言骨架接线（不启 Chromium）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List, Optional

import pytest

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

from app.widgets.card_viewers import CodeWebViewer
from app.widgets.render import BlockCache, RenderCtx

_SRC_VIEWERS = Path(__file__).resolve().parents[2] / "app" / "widgets" / "card_viewers.py"

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

def _make_viewer(monkeypatch, blocks, ctx: Optional[RenderCtx] = None):
    _ensure_qapp()
    v = CodeWebViewer.__new__(CodeWebViewer)
    v._is_js_ready = True
    v._streaming = True
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
    monkeypatch.setattr(CodeWebViewer, "_v2_render_ctx", lambda self: ctx or RenderCtx(streaming=True))
    return v

def _last_js(v) -> str:
    assert v._page.js_calls, "必须下发 JS"
    return v._page.js_calls[-1]

# ── 1. force_full 的滚动保全顺序 ─────────────────────────────────────

def test_force_full_saves_before_clear_and_restores_after_patch(monkeypatch):
    """首渲 force_full：save 必须在清容器之前，restore 在 patch 之后、收尾之前。"""
    v = _make_viewer(monkeypatch, [{"type": "text", "text": "第一段正文"}])
    assert v._perform_update_v2() is True
    js = _last_js(v)

    i_save = js.find("_v2ScrollSave()")
    i_clear = js.find("_c1.innerHTML=''")
    i_patch = js.find("window.patchBlocks('content-placeholder'")
    i_restore = js.find("_v2ScrollRestore()")
    i_after = js.find("_v2AfterPatch(")

    assert i_save >= 0, "整页清空前必须快照滚动位置"
    assert i_clear >= 0
    assert i_restore >= 0, "整页清空后必须还原滚动位置"
    assert i_save < i_clear, "save 必须在清空之前"
    assert i_clear < i_patch, "清空先于 patch"
    assert i_patch < i_restore < i_after, "restore 在 patch 之后、v2 收尾之前"

def test_ctx_sig_change_also_restores_scroll(monkeypatch):
    """工具完成（tool_fingerprints 数量变化）触发 sig 变化 → 同样走 save/restore。

    这是流式期间最高频的整页重建触发点：每完成一个工具清一次容器。
    """
    blocks = [{"type": "text", "text": "正文"}]
    fps = {"set": frozenset()}

    def _ctx():
        return RenderCtx(streaming=True, tool_fingerprints=fps["set"])

    v = _make_viewer(monkeypatch, blocks, ctx=None)
    monkeypatch.setattr(CodeWebViewer, "_v2_render_ctx", lambda self: _ctx())

    assert v._perform_update_v2() is True
    assert "_v2ScrollSave()" in _last_js(v)

    # 工具完成：指纹集合变化 → sig 变化 → force_full
    fps["set"] = frozenset({"tool-x"})
    v._needs_full_render = False
    assert v._perform_update_v2() is True
    js = _last_js(v)
    assert "_c1.innerHTML=''" in js, "sig 变化必须整页重建"
    assert "_v2ScrollSave()" in js and "_v2ScrollRestore()" in js

# ── 2. 骨架接线（源码级） ────────────────────────────────────────────

@pytest.fixture(scope="module")
def src() -> str:
    return _SRC_VIEWERS.read_text(encoding="utf-8")

def test_skeleton_defines_scroll_helpers(src: str):
    assert "window._v2ScrollSave = function" in src
    assert "window._v2ScrollRestore = function" in src
    # 只还原「离开底部」的容器，否则会把跟随态钉死在旧底部
    assert "top >= max - TOL" in src, "restore 必须带底部判定守卫"

def test_collapse_key_prefers_stable_identity(src: str):
    assert "function _collapseKeyOf(block)" in src
    # 稳定键优先序：flip-key（思考块序号）→ tool-call-id → 内容指纹回退
    i = src.find("function _collapseKeyOf(block)")
    body = src[i : i + 700]
    assert "data-flip-key" in body and "data-tool-call-id" in body
    assert "data-block-key" in body, "缺失稳定键时必须回退内容指纹"

    # 三处取键都必须走 _collapseKeyOf，不能直接读 dataset.blockKey
    assert "const key = _collapseKeyOf(block);" in src
    assert "expandedStates.set(_collapseKeyOf(block)" in src
    assert "expandedStates.get(_collapseKeyOf(block))" in src

def test_patch_blocks_freezes_collapsible_transition(src: str):
    """patchBlocks 重建节点时必须冻结 transition，否则展开态重播动画。"""
    i = src.find("window.patchBlocks = function")
    assert i > 0
    body = src[i : i + 9000]
    assert "_v2fz" in body, "patchBlocks 必须冻结折叠框 transition"
    assert "requestAnimationFrame(function () {{ var _e = document.getElementById('_fz'); if (_e) _e.remove(); }})" in body
