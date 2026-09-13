# -*- coding: utf-8 -*-
"""回归测试：消息卡片图表渲染的流式体验与资源回收。

覆盖三个用户可见问题
--------------------
1. **旧图表持续闪烁**：流式每轮 `updateContent` 用 innerHTML 全量重建正文，
   已渲染图表节点被销毁。chart vault 机制本应在重建前把已渲染节点暂存、
   重建后按内容 key 原节点回插（零重建零闪烁），但 `_restoreCharts` 的守卫
   `el._echartInited || el._mmdDone || !el.classList.contains('katex-pending')`
   对 echarts / mermaid 节点**恒为 true**（这两个 class 都不带 katex-pending，
   第三项 `!false` 直接短路）→ 回插从未发生 → 每轮全部图表重新 init/render。
   修复：_chartReady 按节点类型分别判定。

2. **图表一多整卡白屏**：回插失效使得被暂存的节点（标 _chartStashed，绕过
   dispose 兜底）随即被 innerHTML 销毁，echarts 实例 + ResizeObserver 成孤儿，
   流式 N 轮 × M 图滚雪球 → renderer 进程资源耗尽。
   修复：实例生命周期集中到 _disposeChartNode，stash 覆盖 / vault 裁剪 /
   主题切换各路径都必须 dispose + RO disconnect。

3. **流式期间看不到图表在生成**：半截 ```echarts 的残缺 JSON 也被包成
   .echarts-container，JS 侧 JSON.parse 必然失败，只剩 400px 空洞。
   修复：半截 fence 降级为等高骨架（chart-skeleton），闭合后原地切真图。

说明
----
vault 回插逻辑位于注入到 WebEngine 的 JS 中，无法用 pytest 直接执行。除 Python
侧可测的骨架/降级行为外，此处对 JS 关键不变量做**源码级结构断言**：这类 bug
（一行守卫表达式即可让整套机制静默失效）一旦回归，行为测试极难捕获。
"""

import base64
import re
from pathlib import Path

import pytest

from app.widgets import message_card
from app.widgets.message_card import (
    _render_markdown_to_html_cached_impl,
    _sanitize_incomplete_markdown,
    _wrap_code_blocks_with_copy_button_web,
)

_SRC = Path(message_card.__file__).read_text(encoding="utf-8")

HALF_ECHARTS = "前文。\n\n```echarts\n" + '{"title":{"text":"销量"},"series":[{"type":"bar","data":[1,'
FULL_ECHARTS = '```echarts\n{"title":{"text":"销量"},"series":[{"type":"bar","data":[1,2]}]}\n```\n\n后文。'


def code_block(lang, body):
    """构造 markdown 渲染器产出的代码块 HTML（正则要求双引号）。"""
    return f'<pre><code class="language-{lang}">{body}</code></pre>'


# ============ 一、流式骨架（Python 侧可测行为）============


def test_half_echarts_fence_relabelled():
    """半截 echarts fence：语言改 echarts-streaming 并补闭合。"""
    out = _sanitize_incomplete_markdown(HALF_ECHARTS)
    assert "```echarts-streaming" in out, f"语言标记未改: {out!r}"
    assert out.count("```") % 2 == 0, "闭合缺失"


def test_half_chart_block_renders_skeleton():
    """半截 echarts / mermaid 产出等高骨架，不下发渲染属性。"""
    for lang, attr in (("echarts-streaming", "data-echarts-json"), ("mermaid-streaming", "data-mermaid-src")):
        html = _wrap_code_blocks_with_copy_button_web(code_block(lang, "半截内容"))
        assert 'class="chart-skeleton chart-streaming"' in html, f"{lang} 未产出骨架: {html[:160]}"
        assert attr not in html, f"{lang} 半截不应下发 {attr}"


def test_pipeline_half_echarts_no_render_block():
    """管线级：半截 echarts 无渲染块；闭合后渲染块内容完整。"""
    html_half = _render_markdown_to_html_cached_impl(HALF_ECHARTS)
    assert 'data-echarts-json="' not in html_half, "半截不应下发渲染属性"
    assert 'class="chart-skeleton chart-streaming"' in html_half

    html_full = _render_markdown_to_html_cached_impl(FULL_ECHARTS)
    m = re.search(r'data-echarts-json="([^"]+)"', html_full)
    assert m, "闭合后应有渲染块"
    decoded = base64.b64decode(m.group(1)).decode("utf-8")
    assert decoded.startswith('{"title"'), "b64 内容失真"


def test_complete_echarts_untouched():
    """已闭合 echarts：原样渲染，不改语言。"""
    assert _sanitize_incomplete_markdown(FULL_ECHARTS) == FULL_ECHARTS


def test_invalid_json_falls_back_to_code_block():
    """fence 已闭合但 JSON 畸形：降级普通代码块，不留空洞容器。"""
    html = _wrap_code_blocks_with_copy_button_web(code_block("echarts", "{这不是JSON,,,"))
    assert "data-echarts-json" not in html, "非法 JSON 不应下发渲染属性（会留下空洞容器）"
    assert "code-container" in html, "应降级为普通代码块"


def test_plain_code_block_unaffected():
    """普通代码块不受影响（回归）。"""
    html = _wrap_code_blocks_with_copy_button_web(code_block("python", "print(1)"))
    assert "code-container" in html
    assert "chart-skeleton" not in html


# ============ 二、JS 关键不变量（源码级结构断言，防静默回归）============


def test_chart_ready_is_type_aware():
    """_chartReady 必须按节点类型分别判定。

    旧实现 `el._echartInited || el._mmdDone || !el.classList.contains('katex-pending')`
    对 echarts/mermaid 恒为 true → vault 回插全失效（旧图每轮重渲染）。
    """
    m = re.search(r"window\._chartReady = function \(el\) \{\{(.*?)\n                \}\};", _SRC, re.S)
    assert m, "未找到 _chartReady 定义"
    body = m.group(1)
    for cls in ("echarts-container", "mermaid-block", "katex-pending"):
        assert cls in body, f"_chartReady 缺少对 {cls} 的分支判定"
    # 旧的内联短路写法不应残留
    assert "el._echartInited || el._mmdDone ||" not in body, "仍是旧的类型无关短路表达式"


def test_restore_charts_guard_uses_chart_ready():
    """_restoreCharts 的跳过守卫必须走 _chartReady，而非内联表达式。"""
    m = re.search(r"window\._restoreCharts = function \(container\) \{\{(.*?)\n                \}\};", _SRC, re.S)
    assert m, "未找到 _restoreCharts 定义"
    body = m.group(1)
    assert "window._chartReady(el)" in body, "_restoreCharts 守卫未复用 _chartReady"
    assert "el._echartInited || el._mmdDone ||" not in body, "残留旧的内联守卫（回插会再次失效）"


def test_dispose_chart_node_disconnects_resize_observer():
    """_disposeChartNode 必须同时 dispose 实例与 disconnect ResizeObserver。

    RO 对 target 是强引用，只 dispose 图表不断开 RO → 整棵子树常驻。
    """
    m = re.search(r"window\._disposeChartNode = function \(el\) \{\{(.*?)\n                \}\};", _SRC, re.S)
    assert m, "未找到 _disposeChartNode 定义"
    body = m.group(1)
    assert "_chartRO" in body and "disconnect()" in body, "未断开 ResizeObserver"
    assert "dispose()" in body, "未释放 echarts 实例"


def test_vault_eviction_disposes():
    """vault 超限裁剪必须逐个 dispose，不能只 delete Map 条目。"""
    m = re.search(r"window\._restoreCharts = function \(container\) \{\{(.*?)\n                \}\};", _SRC, re.S)
    body = m.group(1)
    evict = re.search(r"while \(window\.__chartVault\.size > 24\) \{\{(.*?)\}\}", body, re.S)
    assert evict, "未找到 vault 裁剪分支"
    assert "_disposeChartNode" in evict.group(1), "裁剪未 dispose → 实例与 RO 永久驻留"


def test_theme_reset_disposes_vault_entries():
    """主题切换清空 vault 时必须逐个 dispose（clear() 会丢引用但不释放资源）。"""
    m = re.search(r"_chart_reset_js = \((.*?)\n                \)", _SRC, re.S)
    assert m, "未找到 _chart_reset_js"
    js = m.group(1)
    assert "_disposeChartNode" in js, "主题切换未 dispose vault 中的实例"
    assert "__chartVault.clear()" in js


def test_skeleton_css_defined():
    """骨架样式必须与真图等高（min-height 300px），保证切换时零布局跳动。"""
    assert ".chart-skeleton {{" in _SRC, "未定义骨架样式"
    idx = _SRC.index(".chart-skeleton {{")
    block = _SRC[idx : idx + 700]
    assert "min-height: 300px" in block, "骨架高度与 .echarts-container 不一致，切换时会跳版"


# ============ 三、echarts init 调度 ============


def test_echarts_pump_uses_time_budget():
    """init 队列用每帧时间预算而非固定每帧 1 个，多图并发出图更跟手。"""
    assert "_ECH_FRAME_BUDGET_MS" in _SRC, "未引入时间预算"
    m = re.search(r"window\._pumpEcharts = function \(\) \{\{(.*?)\n                \}\};", _SRC, re.S)
    assert m, "未找到 _pumpEcharts"
    body = m.group(1)
    assert "while (window.__echQueue.length)" in body, "仍是每帧只取一个节点的旧实现"
    assert "if (window.__echPumpBusy) return;" in body, "缺少并发 rAF 守卫"


def test_echarts_init_reuses_existing_instance():
    """重复扫描同一节点时复用实例（只 setOption），不重复 echarts.init。"""
    m = re.search(r"window\._initOneEcharts = function \(el\) \{\{(.*?)\n                \}\};", _SRC, re.S)
    assert m, "未找到 _initOneEcharts"
    body = m.group(1)
    assert "el._chartInstance" in body and "isDisposed()" in body, "未复用已有实例"
    assert "setOption(option, true)" in body, "应以 notMerge 方式更新，避免残留上一轮 series"


@pytest.mark.parametrize("cls", ["echarts-container", "mermaid-block"])
def test_vault_stash_respects_chart_ready(cls):
    """_stashCharts 用 _chartReady 过滤，未渲染节点不进 vault。"""
    m = re.search(r"window\._stashCharts = function \(container\) \{\{(.*?)\n                \}\};", _SRC, re.S)
    assert m
    assert "if (window._chartReady(el))" in m.group(1)


# ============ 四、骨架节点复用（动画连续性）============


@pytest.mark.parametrize("fn,args", [("updateContentAppend", "(newHtml, tailHtml)"), ("updateTailHtml", "(html)")])
def test_skeleton_taken_before_incremental_removal(fn, args):
    """差量路径必须先摘出骨架，再移除 data-incremental 节点。

    顺序反了骨架就会随 tail 一起被删，每轮新建节点 → CSS 动画重启 →
    用户看到"骨架抖动"而非平滑的生成反馈。
    """
    head = f"function {fn}{args} {{"
    i = _SRC.index(head)
    body = _SRC[i : i + 2200]
    take = body.index("window._takeSkeleton(container)")
    remove = body.index('data-incremental="true"')
    reattach = body.index("window._reattachSkeleton(")
    assert take < remove, f"{fn}: 骨架摘出晚于增量节点移除，动画会每轮重启"
    assert remove < reattach, f"{fn}: 骨架挂回早于增量节点移除，位置会错乱"


def test_reattach_drops_skeleton_when_chart_closes():
    """fence 闭合（本轮出现真图）时骨架必须退场，否则真图下方残留占位。"""
    m = re.search(r"window\._reattachSkeleton = function \([^)]*\) \{\{(.*?)\n                \}\};", _SRC, re.S)
    assert m, "未找到 _reattachSkeleton 定义"
    body = m.group(1)
    assert "_hasRealChartIn" in body, "未判断本轮是否已产出真图"


def test_has_real_chart_covers_both_chart_types():
    """_hasRealChartIn 需同时识别 echarts 与 mermaid 的真图容器。"""
    m = re.search(r"window\._hasRealChartIn = function \(html\) \{\{(.*?)\n                \}\};", _SRC, re.S)
    assert m, "未找到 _hasRealChartIn 定义"
    body = m.group(1)
    for cls in ("echarts-container", "mermaid-block"):
        assert cls in body, f"_hasRealChartIn 未覆盖 {cls}"


def test_skeleton_reuse_clears_duplicate():
    """复用旧骨架时必须清掉 tail 内新渲染的同款骨架，否则出现双骨架。"""
    m = re.search(r"window\._reattachSkeleton = function \([^)]*\) \{\{(.*?)\n                \}\};", _SRC, re.S)
    body = m.group(1)
    assert ".chart-skeleton" in body and "removeChild" in body, "未清除重复骨架节点"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
