# -*- coding: utf-8 -*-
"""回归测试：渲染型 fence（```echarts 等）流式生肉泄漏（代码打出后消失）。

现象（用户报告）
----------------
图表 fence 流式输出期间，JSON/源码以生肉形式逐行显示；fence 闭合触发
全量渲染后被图表卡片替换，代码又消失。观感差。

根因（与 think/mood 未闭合泄漏同族）
------------------------------------
渲染型 fence（echarts/mermaid/html/widget/svg + 插件 fence 注册表）闭合后
由全量渲染分发为 chart-streaming 骨架/真图；但未闭合期间：
1. append_text 仍调 _append_text_incremental → 代码生肉进 DOM；
2. _apply_render_result 基线推进用 rfind("\\n\\n")，fence 内 JSON 含空行时
   stable 落进 fence 内部 → 差量切片把内部代码行当普通段落产出渲染。

修复
----
- _has_unclosed_chart_fence：检测文本停在未闭合渲染型 fence 内；
- append_text 未闭合渲染型 fence 期间跳过增量注入（普通代码块不受影响）；
- _last_para_break_outside_fence：基线推进点 fence 感知，stable 永不越过
  未闭合 fence 起点；
- _render_tail_inline / _push_unrendered_tail_text 守卫同步补齐。
"""

from app.widgets.message_card import (
    _extract_closed_segments,
    _has_unclosed_chart_fence,
    _last_para_break_outside_fence,
)


def test_has_unclosed_chart_fence_basic():
    assert not _has_unclosed_chart_fence(""), "空串应为 False"
    assert not _has_unclosed_chart_fence("正文没有 fence"), "无 fence 应为 False"
    assert _has_unclosed_chart_fence("```echarts\n{\"x\":1}"), "未闭合 echarts 应为 True"
    assert not _has_unclosed_chart_fence("```echarts\n{\"x\":1}\n```\n正文"), "已闭合应为 False"
    assert _has_unclosed_chart_fence("```echarts\noption = 1\n\nseries = 2"), "含空行未闭合应为 True"


def test_has_unclosed_chart_fence_plain_code_not_matched():
    """普通代码块（python 等）不拦：流式生肉显示是预期行为。"""
    assert not _has_unclosed_chart_fence("```python\nprint(1)"), "普通代码块不应命中"
    assert not _has_unclosed_chart_fence("```\n无语言 fence"), "无语言 fence 不应命中"
    assert not _has_unclosed_chart_fence("```python\nx = 1\n```\n正文"), "已闭合普通块不算"
    assert _has_unclosed_chart_fence("```python\nx=1\n```\n```mermaid\ngraph TD"), "第二个渲染型未闭合应为 True"


def test_has_unclosed_chart_fence_mixed_sequence():
    """先普通块后图表块、交错序列。"""
    md = "```python\na=1\n```\n\n正文\n\n```mermaid\ngraph TD;\nA-->B"
    assert _has_unclosed_chart_fence(md), "mermaid 未闭合应为 True"
    md2 = "```echarts\n{}\n```\n\n正文"
    assert not _has_unclosed_chart_fence(md2), "echarts 已闭合应为 False"


def test_last_para_break_outside_fence():
    """基线推进点不落进 fence 内部。"""
    # 无 fence：等价 rfind
    assert _last_para_break_outside_fence("a\n\nb") == 1
    # fence 未闭合且内部含空行：推进点应在 fence 之前
    md = "前文\n\n```echarts\n{\"a\":1,\n\n\"b\":2}"
    pos = _last_para_break_outside_fence(md)
    assert pos < md.find("```echarts"), f"推进点不应越过 fence 起点: {pos}"
    # fence 已闭合：可推进到闭合后的空行
    md2 = "```echarts\n{}\n```\n\n后文\n\n结尾"
    pos2 = _last_para_break_outside_fence(md2)
    assert pos2 == md2.rfind("\n\n"), f"闭合后应等价 rfind: {pos2}"


def test_extract_closed_segments_fence_internal_start():
    """切片起点在 fence 内部（stable 落在 fence 内的历史遗留）时不产出内部段。"""
    # fence 感知推进后 stable 不应落进 fence 内；此用例锁定差量切段对
    # "fence 跨空行整块产出" 的既有行为未被破坏。
    md = "正文\n\n```echarts\n{\"a\":1,\n\n\"b\":2}\n```\n\n正文二"
    stable, segs = _extract_closed_segments(md)
    joined = "\n\n".join(segs)
    assert stable > 0 and segs, "应产出闭合段"
    assert '"a":1' in joined, "fence 整块应作为闭合段产出（含内部空行）"


# ── chunk 边界切开标记的半截拦截 ──
def test_partial_fence_tail_intercepted():
    """chunk 把 fence 开标记切成半截（尾部 "```e" / "``"）时必须拦截。"""
    assert _has_unclosed_chart_fence("正文\n\n```e"), "半截 lang 前缀应拦截"
    assert _has_unclosed_chart_fence("正文\n\n``"), "半截反引号应拦截"
    assert _has_unclosed_chart_fence("正文\n\n```echarts"), "完整标记无内容也应拦截（inside）"


def test_partial_tag_tail_intercepted():
    """chunk 把 <mood> 切成半截（尾部 "<mo"）时必须拦截。"""
    from app.widgets.message_card import _has_unclosed_registered_tag

    assert _has_unclosed_registered_tag("正文<mood>a</mood>\n下一行<mo"), "半截标签应拦截"
    assert not _has_unclosed_registered_tag("正文<mood>a</mood>"), "完整闭合不受影响"


def test_first_unclosed_chart_fence_pos():
    """未闭合渲染型 fence 的位置计算：截断点在 fence 开标记处。"""
    from app.widgets.message_card import _first_unclosed_chart_fence_pos

    md = "正文一\n\n```echarts\n{\"a\":1"
    pos = _first_unclosed_chart_fence_pos(md)
    assert pos == md.find("```echarts"), f"应指向 fence 开标记: {pos}"
    assert _first_unclosed_chart_fence_pos("```echarts\n{}\n```\n正文") == -1, "闭合后应为 -1"
    assert _first_unclosed_chart_fence_pos("```python\nx=1") == -1, "普通代码块不拦"


def test_tail_before_unclosed_block_cuts_at_fence():
    """差量 tail 截断：未闭合 fence 之前的正文保留，fence 起点之后静默。"""
    from app.widgets.message_card import _tail_before_unclosed_block

    tail = "正文一\n\n```echarts\n{\"a\":1"
    cut = _tail_before_unclosed_block(tail)
    assert cut == "正文一\n\n", f"应截到 fence 开标记: {cut!r}"

# ── 流式图表两遍（2026-10-30 用户实测）──────────────────────────────
# 现象：图表 fence 闭合瞬间触发全量渲染（DOM 里图表 A），但 `_stable_md_len`
# 停在 fence 起点（尾部无空行，`_last_para_break_outside_fence` 只能停在这里）
# → 下一个 chunk 到达后差量把同一 fence 当"新闭合段"再产出一次（图表 B），
# 旧节点 A 不是增量节点不会被移除 → 同一张卡片两个图表；流式结束全量重建才
# 收敛成一个。
_CHART_FENCE = """```echarts
{"title": {"text": "UI 评分雷达"}, "series": []}
```"""

def test_last_closed_fence_end_basic():
    from app.widgets.card_render_core import _last_closed_fence_end

    md = "前文\n\n" + _CHART_FENCE
    assert _last_closed_fence_end(md) == len(md), "闭合行结束即 md 末尾"
    md2 = "前文\n\n" + _CHART_FENCE + "\n\n后文"
    assert _last_closed_fence_end(md2) == md2.find("后文") - 1, "应指向闭合行换行之后（空行前）"
    assert _last_closed_fence_end("```echarts\n未闭合") == -1, "未闭合 fence 不算"
    assert _last_closed_fence_end("正文没有 fence") == -1

def test_stream_stable_len_crosses_closed_fence():
    from app.widgets.card_render_core import _compute_stream_stable_len

    md = "前言一段。\n\n" + _CHART_FENCE
    assert _compute_stream_stable_len(md) == len(md), "已闭合 fence 属稳定区，基线必须越过"
    md2 = "前言。\n\n```echarts\n{\"a\":1}"
    assert _compute_stream_stable_len(md2) <= md2.find("```echarts"), "未闭合 fence 不得越过"

def test_stream_stable_no_duplicate_chart_after_full_render():
    """回归：全量渲染后基线越过闭合 fence，下一轮差量不得再产出该 fence。"""
    from app.widgets.card_render_core import (
        _compute_stream_stable_len,
        _extract_closed_segments,
        _render_stable_segment,
    )

    md = "前言一段。\n\n" + _CHART_FENCE
    full_html = _render_stable_segment(md, compact=False)
    assert full_html.count("echarts-container") == 1
    stable = _compute_stream_stable_len(md)
    md_next = md + "\n\n**三个硬伤**"
    _stable_len, segs = _extract_closed_segments(md_next[stable:])
    extra = "".join(_render_stable_segment(s, compact=False) for s in segs)
    assert "echarts-container" not in extra, "已渲染的 fence 不得作为新差量段重复产出"

def test_chart_ids_in_extracts_container_ids():
    from app.widgets.card_render_core import _chart_ids_in

    html = (
        '<p>正文</p><div id="echart-0123456789ab" class="echarts-container"></div>'
        '<div id="mmd-abcdef123456" class="mermaid-block"></div>'
    )
    assert _chart_ids_in(html) == frozenset({"echart-0123456789ab", "mmd-abcdef123456"})
    assert _chart_ids_in("<p>没有图表</p>") == frozenset()
    assert _chart_ids_in("") == frozenset()
    # 同内容 → 同 id：全量渲染与尾部行内渲染产物必须能对上，否则守卫失效
    assert _chart_ids_in('<div id="echart-0123456789ab"></div>') & _chart_ids_in(html)
