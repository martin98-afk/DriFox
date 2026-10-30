# -*- coding: utf-8 -*-
"""纯函数级复现：全量渲染后 stable 停在闭合 fence 之前 → 下一轮差量重复产出该 fence。

模拟 v1 流式管线（card_viewers._perform_update 差量分支）的 DOM 语义：
- 全量渲染：dom 清空后写入整篇产物（图表容器 A）
- 差量渲染：把 md[stable:] 的闭合段追加进 dom（不清已有非增量节点）
- 基线推进：stable = _last_para_break_outside_fence(md) + 2
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.widgets.card_render_core import (
    _compute_stream_stable_len,
    _extract_closed_segments,
    _last_closed_fence_end,
    _last_para_break_outside_fence,
    _render_stable_segment,
)

CHART = """```echarts
{"title": {"text": "UI 评分雷达"}, "series": [{"type": "radar", "data": [{"value": [9, 8]}]}]}
```"""

def full_render(md: str) -> tuple[list[str], int]:
    """全量渲染：返回 (dom 节点 html 列表, 新 stable)。"""
    html = _render_stable_segment(md, compact=False)
    stable = _compute_stream_stable_len(md)
    return [html], stable

def diff_render(md: str, stable: int, dom: list[str]) -> list[str]:
    """差量渲染：闭合段追加到 dom，尾部节点替换（语义简化，不计 tail）。"""
    stable_len, segs = _extract_closed_segments(md[stable:])
    if not segs:
        return dom
    new_html = [_render_stable_segment(seg, compact=False) for seg in segs]
    return dom + new_html

def charts(dom: list[str]) -> int:
    return sum(h.count("echarts-container") for h in dom)

md = "前言一段说明。\n\n"
dom, stable = full_render(md + CHART)  # 模拟：fence 闭合瞬间触发全量渲染
md = md + CHART
print(f"[1] 全量渲染 md={len(md)} stable={stable} 图表={charts(dom)}")
print(f"    stable 之后仍留在 tail 的内容: {md[stable:60 + stable]!r}")

# 下一个 chunk 到达（图表后跟正文）
md += "\n\n**三个硬伤（布局架构层）**"
dom2 = diff_render(md, stable, dom)
print(f"[2] 差量追加后 图表={charts(dom2)} 节点数={len(dom2)}")
for i, h in enumerate(dom2):
    print(f"    node{i}: charts={h.count('echarts-container')} len={len(h)} head={h[:50]!r}")

if charts(dom2) > 1:
    print("\n⚠️ 复现：同一张卡片 DOM 中出现两个图表容器（流式两遍）")
else:
    print("\n✅ 未复现（修复生效：基线越过已闭合 fence）")

# 边界用例：_last_closed_fence_end 与复合 stable 推进
EDGE = {
    "fence 结尾无换行": "前言。\n\n```echarts\n{}\n```",
    "fence 结尾带换行": "前言。\n\n```echarts\n{}\n```\n",
    "fence 后有正文": "前言。\n\n```echarts\n{}\n```\n\n后续。",
    "未闭合 fence": "前言。\n\n```echarts\n{\"a\": 1}",
    "闭合后又有未闭合": "甲。\n\n```js\nx\n```\n\n```echarts\n{",
}
print("\n---- 边界 ----")
for name, md in EDGE.items():
    fe = _last_closed_fence_end(md)
    st = _compute_stream_stable_len(md)
    print(f"  [{name}] fence_end={fe} stable={st} tail={md[st:st+24]!r}")
