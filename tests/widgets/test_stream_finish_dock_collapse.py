# -*- coding: utf-8 -*-
"""回归：流式结束「坞态归位 + 工具区折叠」两处终态收敛

缺陷（2026-10-17 用户反馈）：简洁模式下对话结束，卡片停留流式形态 ——
底部左右移动的光条不消失、工具与思考区不回顶部、也不折叠。

根因两处，都在 JS 侧，缺一不可：

1. `_setStreamingDock(active, collapseAfter)` 的折叠块挂在
   ``on === false && wasOn`` 分支，且位于 ``if (on === wasOn) return;``
   **之后**。打断 / 插话 / 错误收尾会重复调用
   `_sync_streaming_dock(False, collapse_after=True)`：首次 finish 已归位，
   二次收尾（force_dock_off）时 wasOn=false → 先被幂等 return 挡住，
   即便进了也被 wasOn 判据否掉 → 折叠永不置位。
   折叠是「终态属性」而非「切换副作用」，必须提到幂等 return 之前、
   且判据只看目标态。

2. `_updateToolSectionHeader()` 的自动展开只看 DOM 里有没有
   `data-streaming="true"` 的块，不看卡片是否还在流式。打断后 worker 已终止，
   未完成工具的运行框恒带该属性 → 此后每次全量渲染把刚折叠的工具区掰回展开。
   判据补 `window._streamingActive`。

本文件只锁这两条 JS 资产的**源码契约**（字符串级断言，无需起 WebEngine）：
真机 DOM 行为由 tests/debug/_tmp_finish_state_probe.py 类探针覆盖。
"""

import re

from app.widgets import card_render_core as crc  # noqa: E402
from app.widgets import card_viewers as cv  # noqa: E402


def _js_asset(name: str) -> str:
    return getattr(crc, name)


# ─── 1. _setStreamingDock：折叠必须在幂等 return 之前 ───────────────


def test_collapse_precedes_idempotent_return():
    """折叠块必须位于 `if (on === wasOn) return;` 之前

    否则二次收尾（打断/插话 force_dock_off）连函数体都进不去，折叠永不置位。
    """
    js = _js_asset("_STREAMING_DOCK_JS")
    # ⚠️ 注释里也出现了这句代码文本，必须用行级定位取**可执行**的那一行
    lines = js.splitlines()
    exec_idx = [i for i, ln in enumerate(lines) if ln.strip() == "if (on === wasOn) return;"]
    assert exec_idx, "未找到可执行的幂等 return 行"
    i_return = exec_idx[0]
    collapse_idx = [
        i for i, ln in enumerate(lines) if "_tsC0.setAttribute('data-collapsed', 'true')" in ln
    ]
    assert collapse_idx, "未找到折叠置位行"
    i_collapse = collapse_idx[0]
    assert i_collapse < i_return, f"折叠块(行{i_collapse}) 必须早于幂等 return(行{i_return})"


def test_collapse_condition_excludes_was_on():
    """折叠判据不得再依赖 wasOn（否则二次收尾被判据否掉）"""
    js = _js_asset("_STREAMING_DOCK_JS")
    # 定位折叠块的条件行
    m = re.search(r"if \(!on && collapseAfter[^\n]*\) \{", js)
    assert m, "未找到折叠块条件"
    cond = m.group(0)
    assert "wasOn" not in cond, f"折叠判据仍含 wasOn（二次收尾会被否掉）: {cond}"
    assert "collapseAfter" in cond
    assert "_toolCompactMode" in cond


def test_no_duplicate_collapse_after_toggle():
    """class toggle 之后不得再有第二处折叠置位（同帧重复会重启多余过渡）"""
    js = _js_asset("_STREAMING_DOCK_JS")
    lines = js.splitlines()
    toggle_idx = [i for i, ln in enumerate(lines) if "classList.toggle('streaming-dock', on)" in ln]
    assert toggle_idx, "未找到 class toggle 行"
    i_toggle = toggle_idx[0]
    tail = lines[i_toggle:]
    assert sum(1 for ln in tail if "setAttribute('data-collapsed', 'true')" in ln) == 0, (
        "toggle 之后仍有折叠置位：同帧连置两次会重启一次多余的 max-height 过渡"
    )


# ─── 2. _updateToolSectionHeader：自动展开必须看卡片是否还在流式 ──────


def test_auto_expand_guarded_by_streaming_active():
    """自动展开必须带 window._streamingActive 判据

    已结束卡片里未完成的工具运行框恒带 data-streaming="true"，无此判据时
    每次全量渲染都会把刚折叠的工具区掰回展开。
    """
    src = open(cv.__file__, encoding="utf-8").read()
    m = re.search(r"if \(window\._streamingActive && _hasStreaming[^\n]*\) \{", src)
    assert m, "自动展开未带 _streamingActive 判据：结束态折叠会被反复掰回"


def test_streaming_active_cleared_on_finish():
    """_setStreamingDock 必须无条件维护 window._streamingActive（供上条判据用）"""
    js = _js_asset("_STREAMING_DOCK_JS")
    assert "window._streamingActive = !!active;" in js
    # 复位也在复用清理里，避免上一张卡片的流式标志污染新卡
    reset = _js_asset("_RESET_CONTENT_FOR_REUSE_JS")
    assert "window._streamingActive = false;" in reset
