# -*- coding: utf-8 -*-
"""v2 通道「正式块」身份判据回归测试。

背景（2026-10-29 真机现象）：流式结束后编辑类工具（write/edit/multi_edit）的
完成框沉底残留在正文末尾。

根因链：
1. ``MessageCard.append_tool_result`` 把完成框 JS 注入 ``#content-placeholder``，
   注入节点**带** ``data-block-key``（值来自 ``render_tool_block`` 内层指纹
   ``tool-<sha1>``，与 patch 用的 key 不同尺度）；
2. ``window._blockNodes`` 按 ``hasAttribute('data-block-key')`` 判正式块 → 注入块
   被算进块序列（patch 的 base 索引错位）；
3. ``window._v2AfterPatch`` 去重同样按 key 判定 → 注入块被当成正式块跳过 →
   同 id 的重复框永不删除；编辑类工具又因 ``data-keep-in-content`` 不走搬移分支，
   只能靠去重清 → 永久沉底。

修复：判据统一改 ``data-block-wrapper``（``patchBlocks`` 包装层专有）。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "app/widgets/card_viewers.py").read_text(encoding="utf-8")


def _block_nodes_body() -> str:
    i = SRC.index("window._blockNodes = function (container)")
    return SRC[i : i + 900]


def _after_patch_body() -> str:
    i = SRC.index("window._v2AfterPatch = function (streaming)")
    return SRC[i : i + 4000]


def test_block_nodes_only_counts_wrappers():
    """块序列只认 patchBlocks 包装层，注入的完成框不得参与 splice 索引。"""
    body = _block_nodes_body()
    assert "data-block-wrapper" in body
    assert "hasAttribute('data-block-key')" not in body, (
        "按 data-block-key 判定会把 JS 注入的完成框算进块序列（它也带该属性）→ patch base 错位"
    )


def test_after_patch_dedup_uses_wrapper_identity():
    """去重：正式块身份用包装层判定，删除段跳过包装层。"""
    body = _after_patch_body()
    assert "kb.hasAttribute('data-block-wrapper')" in body, "id 收集必须只认包装层"
    assert "k2.hasAttribute('data-block-wrapper')) continue" in body, "删除段必须跳过包装层"


def test_after_patch_collects_self_and_descendant_ids():
    """正式块的 data-tool-call-id 可能在外层自身，也可能在 wrapper 内层。"""
    body = _after_patch_body()
    assert "_selfTid" in body, "块自身携带的 data-tool-call-id 必须登记"
    assert "querySelectorAll('[data-tool-call-id]')" in body, "wrapper 内层的 id 也必须登记"


def test_skeleton_version_bumped():
    """骨架 JS 变更必须递增缓存版本，否则旧骨架继续跑旧判据。"""
    core = (ROOT / "app/widgets/card_render_core.py").read_text(encoding="utf-8")
    i = core.index("_SKELETON_CACHE_VERSION = ")
    version = int(core[i : i + 80].split("=")[1].split("\n")[0].strip())
    assert version >= 55, f"v2 正式块判据变更需 >= v55，当前 {version}"
