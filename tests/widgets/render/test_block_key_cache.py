# -*- coding: utf-8 -*-
"""block_key 指纹驻留（perf R1b）的行为测试。

驻留契约：失据（内容引用 / completed / compact / fp_tag）未变 → 直返驻留值
且不重算 sha1；任一失据变化 → key 必须跟着变，不得复用过期驻留。
不需要 QApplication：纯字符串计算。
"""

from __future__ import annotations

import app.widgets.render.block_render as br
from app.widgets.render.blocks import BlockRender, PatchOps, RenderCtx
from app.widgets.render.block_render import block_key

CTX = RenderCtx()


def _spy_fingerprint(monkeypatch):
    """把 block_render.fingerprint 换成计数桩，返回调用记录列表。"""
    import app.widgets.render.blocks as blocks_mod

    calls = []
    real = blocks_mod.fingerprint

    def spy(*parts):
        calls.append(parts)
        return real(*parts)

    monkeypatch.setattr(br, "fingerprint", spy)
    return calls


def test_repeat_call_same_key_and_skips_recompute(monkeypatch):
    calls = _spy_fingerprint(monkeypatch)
    block = {"type": "reasoning", "content": "推理内容" * 50}
    k1 = block_key(block, CTX, True)
    assert len(calls) == 1
    k2 = block_key(block, CTX, True)
    assert k2 == k1
    assert len(calls) == 1  # 第二次命中驻留，不重算


def test_content_change_invalidates(monkeypatch):
    calls = _spy_fingerprint(monkeypatch)
    block = {"type": "reasoning", "content": "旧内容"}
    k1 = block_key(block, CTX, True)
    block["content"] = "旧内容" + "新追加"
    k2 = block_key(block, CTX, True)
    assert k2 != k1
    assert len(calls) == 2


def test_equal_length_swap_invalidates():
    """等长替换（id 地址可能复用 + len 相同）不得复用旧驻留。"""
    block = {"type": "reasoning", "content": "aaaa"}
    k1 = block_key(block, CTX, True)
    block["content"] = "bbbb"
    assert block_key(block, CTX, True) != k1


def test_completed_change_invalidates():
    block = {"type": "text", "text": "正文"}
    k1 = block_key(block, CTX, False)
    k2 = block_key(block, CTX, True)
    assert k2 != k1


def test_compact_change_invalidates():
    """简洁模式是运行时可切换配置，驻留必须跟着失效。"""
    block = {"type": "reasoning", "content": "内容"}
    k1 = block_key(block, CTX, True)
    k2 = block_key(block, RenderCtx(compact=True), True)
    assert k2 != k1


def test_tool_fingerprints_change_invalidates():
    """text 块含 <tool> 时 fp_tag 参与指纹，抑制集规模变化须失效。"""
    block = {"type": "text", "text": "带 <tool> 的文本"}
    k1 = block_key(block, CTX, True)
    k2 = block_key(block, RenderCtx(tool_fingerprints=frozenset({"fp1"})), True)
    assert k2 != k1


def test_tool_streaming_preview_change_invalidates():
    block = {"type": "tool_streaming", "tool_call_id": "t1", "name": "bash", "preview": "ls"}
    k1 = block_key(block, CTX, False)
    block["preview"] = "ls -la"
    k2 = block_key(block, CTX, False)
    assert k2 != k1


def test_bk_never_leaks_into_payload():
    """_bk 只驻留在块 dict，不进 JS 序列化产物。"""
    payload = PatchOps(base=0, delete=1, items=(BlockRender(kind="text", key="k", html="<p>x</p>"),)).to_payload()
    assert set(payload["items"][0].keys()) == {"kind", "key", "html", "home"}
    block = {"type": "text", "text": "x"}
    block_key(block, CTX, True)
    assert "_bk" in block  # 驻留确实写上了
    assert all("_bk" not in it for it in payload["items"])
