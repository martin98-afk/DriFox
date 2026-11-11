# -*- coding: utf-8 -*-
"""「工具与思考」区自动展开必须受流式态守卫。

背景（[T44] 之后）：工具/思考区固定在正文上方，工具区高度变化 = 正文整体位移。
流式结束时折叠由 ``_setStreamingDock(false, collapse_after=True)`` 同帧完成，
而终渲染延后 ``DOCK_COLLAPSE_SETTLE_MS`` 派发；若终渲染里的
``_updateToolSectionHeader`` 只看 ``data-streaming="true"``（DOM 里可能残留
注入运行框 / 尚未正式化的思考块），就会把刚折叠好的工具区重新顶开 ——
正文先上移再下移数百 px，即用户实测的「结束瞬间文字上下抖动」。

守卫生效范围：``window._streamingActive`` 由 ``_setStreamingDock`` 维护，
流式期间为 True（含 keep_dock 路径，工具仍在跑时不调 dock off），
结束后为 False。历史卡/复用卡初始为 False，保持折叠终态。
"""

from pathlib import Path

_WIDGETS = Path(__file__).resolve().parents[2] / "app" / "widgets"
_CARD_VIEWERS = _WIDGETS / "card_viewers.py"
_CARD_RENDER_CORE = _WIDGETS / "card_render_core.py"

_MARKER = "自动展开：流式时有新工具且当前折叠"


def _auto_expand_block() -> str:
    src = _CARD_VIEWERS.read_text(encoding="utf-8")
    start = src.index(_MARKER)
    return src[start : start + 1200]


def test_auto_expand_guarded_by_streaming_active():
    """自动展开分支必须与 window._streamingActive 取与。"""
    block = _auto_expand_block()
    assert "window._streamingActive" in block, (
        "自动展开必须受流式态守卫：结束终渲染残留的 data-streaming 会把已折叠的工具区顶开"
    )
    assert "_hasStreaming" in block, "守卫后仍应保留 data-streaming 判据"
    assert "_tsEl.setAttribute('data-collapsed', 'false')" in block, "展开动作本身不应改动"


def test_auto_expand_still_checks_dom_streaming_block():
    """守卫只是追加条件，不能退化成无条件展开。"""
    block = _auto_expand_block()
    idx = block.index("_hasStreaming")
    seg = block[idx : idx + 300]
    assert "data-streaming" in seg, "展开仍须要求工具区里真的有流式块"


def test_skeleton_version_bumped():
    """骨架 JS 变更必须递增 _SKELETON_CACHE_VERSION（否则磁盘缓存骨架不失效）。"""
    src = _CARD_RENDER_CORE.read_text(encoding="utf-8")
    idx = src.index("_SKELETON_CACHE_VERSION = ")
    tail = src[idx : idx + 40]
    version = int(tail.split("=", 1)[1].strip().split("\n", 1)[0])
    assert version >= 64, f"自动展开守卫上线后骨架版本必须 >=64，实际 {version}"
