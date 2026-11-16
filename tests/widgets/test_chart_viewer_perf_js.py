# -*- coding: utf-8 -*-
"""ChartViewerCard echarts 放大查看页的卡顿防护接线。

真窗 harness（tests/debug/chart_viewer_perf_harness.py）实测：
  - 20 次容器 resize 触发 13 次 chart.resize()，单次冻结 74~130ms（p95 74~107ms）
  - dataZoom inside 滚轮缩放稳态 p95≈34ms（≈30fps）
  - QScrollArea / canvas 面积 / panZoom 监听三组对照无显著差异（H1/H2/H3 全证伪）

修法落在 build_chart_viewer_html 的 echarts JS：
  ① window resize → chart.resize 合并到 rAF 一帧一次（防抖）
  ② 注入的 dataZoom inside 关掉缩放动画（animationDurationOnZoom: 0）
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _echarts_html() -> str:
    import base64
    import sys

    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from app.widgets.cards.settings.chart_viewer_card import build_chart_viewer_html

    option = '{"xAxis":{"type":"category","data":["a","b"]},"yAxis":{},"series":[{"type":"line","data":[1,2]}]}'
    return build_chart_viewer_html("echarts", base64.b64encode(option.encode()).decode())


def test_echarts_resize_is_coalesced_to_one_raf_per_frame():
    """resize 事件不得直接同步调 chart.resize（20 次 resize → 13 次 74-130ms 冻结）。"""
    html = _echarts_html()
    assert "addEventListener('resize', function () { chart && chart.resize(); });" not in html
    assert "requestAnimationFrame" in html
    assert "_resizePending" in html


def test_echarts_update_animation_disabled():
    """dataZoom 拖拽 / resize 走 setOption update 路径，须在 option 顶层关补间动画。

    注意：animationDurationOnZoom 不是 echarts 合法键（vendor 里不存在），
    塞进 dataZoom 组件项无效；唯一生效位置是 option 顶层。
    """
    html = _echarts_html()
    assert "option.animationDurationUpdate = 0;" in html
    assert "animationDurationOnZoom" not in html


def test_wheel_not_hijacked_by_datazoom():
    """放大页普通滚轮不得被 dataZoom inside 劫持成全量重绘缩放。

    真窗单变量对照（tests/debug/_probe_dz_variant.py）：
      zoomOnMouseWheel:true  → wheel p95 33.7ms，>33ms 重帧 11，datazoom 事件 20
      zoomOnMouseWheel:false → wheel p95 17.8ms，>33ms 重帧 1，datazoom 事件 0
    缩放能力保留在底部 slider，滚轮回归页面滚动。
    """
    html = _echarts_html()
    assert "zoomOnMouseWheel: false" in html
    assert "moveOnMouseMove: false" in html
    assert "zoomOnMouseWheel: true" not in html
    assert "type: 'slider'" in html  # 缩放能力不丢失


def test_other_chart_modes_unaffected():
    """mermaid/svg/html 三种模式不引入 echarts 的 resize 防抖逻辑。"""
    import base64
    import sys

    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from app.widgets.cards.settings.chart_viewer_card import build_chart_viewer_html

    payload = base64.b64encode(b"<svg/>").decode()
    for ctype in ("mermaid", "svg", "html"):
        html = build_chart_viewer_html(ctype, payload)
        assert "_resizePending" not in html, f"{ctype} 不应引入 echarts 专用逻辑"
