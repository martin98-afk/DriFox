# -*- coding: utf-8 -*-
"""回归：macOS 打包版图表空白 / SVG 围栏降级为源码代码块。

三个用户可见症状，同一份可视化回复在 Windows 正常、macOS 打包版异常：

1. `` ```echarts `` 只剩 400px 空框：build.py 漏打 echarts 两个 vendor，
   打包版走 CDN 降级；CDN 不通时 `_echLoadScript` 的 onerror 只打日志，
   不清 `_echLibLoading`、不清回调队列，容器永久停在空框且后续图表全饿死。
2. `` ```svg `` 以 `<style>` 开头时 `startswith("<svg")` 判定失败 → 降级成
   带行号的源码代码块。visualization skill 的 svg-guide 明确要求「SVG 前放
   一段标准 style」，宿主兜底却要求首字符即 `<svg`，契约冲突。

JS 侧逻辑注入在 WebEngine，pytest 无法直接执行；按项目既有做法对骨架源码
做结构断言（见 test_message_card_chart_streaming.py 第二、三节）。
"""

import re
from pathlib import Path

import pytest

from app.widgets import card_render_core, card_viewers
from app.widgets.card_render_core import _render_render_fence

_CARD_VIEWERS_SRC = Path(card_viewers.__file__).read_text(encoding="utf-8")
_BUILD_SRC = (
    Path(__file__).resolve().parents[2] / "build.py"
).read_text(encoding="utf-8")


# ============ 一、SVG 围栏：style 前置仍应透传 ============

SVG_WITH_LEADING_STYLE = (
    '<style>\n.t{font:400 13px sans-serif;fill:var(--text)}\n</style>\n'
    '<svg viewBox="0 0 680 200" width="100%" role="img">\n'
    '  <rect x="40" y="40" width="600" height="120" rx="12"/>\n'
    '</svg>'
)


def test_svg_fence_with_leading_style_is_inlined():
    """技能文档要求 style 前置；宿主必须识别内含的完整 <svg> 而非只看首字符。"""
    out = _render_render_fence("svg", SVG_WITH_LEADING_STYLE)
    assert out is not None, "style 前置的 SVG 被降级成普通代码块"
    assert "<svg" in out and "</svg>" in out
    assert "<style>" in out


def test_svg_fence_starting_with_svg_still_inlined():
    """红线：原本以 <svg 开头的写法行为不变。"""
    body = SVG_WITH_LEADING_STYLE[SVG_WITH_LEADING_STYLE.index("<svg"):]
    out = _render_render_fence("svg", body)
    assert out is not None and "<svg" in out


def test_svg_fence_teaching_code_not_swallowed():
    """红线：讲 SVG 语法的示例代码（不含完整 svg 元素）仍走代码块。"""
    out = _render_render_fence("svg", "SVG 里用 <svg> 标签包裹图形，viewBox 定义坐标系。")
    assert out is None, "教学片段被误吞成真实 DOM"


def test_svg_fence_incomplete_svg_not_inlined():
    """流式半截：<svg 开但没有 </svg>，透传会撕碎后续 DOM，必须降级。"""
    out = _render_render_fence("svg", '<style>.t{}</style>\n<svg viewBox="0 0 680 200">')
    assert out is None


# ============ 二、ECharts vendor 加载失败：不得静默空框 ============

def test_echarts_load_error_resets_loading_flag_and_queue():
    """onerror 必须清 `_echLibLoading` 与 `_echLibQueue`。

    否则一次 CDN 失败后 `_echLibLoading` 恒为 true，后续所有图表永久排队、
    容器停在 400px 空框（macOS 打包版实测）。
    """
    m = re.search(
        r"function _echLoadScript\(src, onOk\) \{(.*?)\n                \}\}",
        _CARD_VIEWERS_SRC,
        re.S,
    )
    assert m, "未找到 _echLoadScript 定义"
    body = m.group(1)
    assert "onerror" in body, "缺少加载失败分支"
    # onerror 分支内必须把加载锁复位并清空等待队列
    err = body[body.index("onerror"):]
    assert "_echLibLoading = false" in err, "加载失败未清 _echLibLoading（后续图表全饿死）"
    assert "_echLibQueue = []" in err, "加载失败未清等待队列"


def test_echarts_load_error_marks_containers_visible():
    """加载失败不能只 console.error：要把容器标成失败态，让用户看到原因。"""
    m = re.search(
        r"function _echLoadScript\(src, onOk\) \{(.*?)\n                \}\}",
        _CARD_VIEWERS_SRC,
        re.S,
    )
    err = m.group(1)
    err = err[err.index("onerror"):]
    assert "echarts-failed" in err, "加载失败未给容器打失败标记（用户只看到空框）"


def test_echarts_failed_css_has_hint_text():
    """失败态 CSS 必须给出可读提示；现有文案是「等待生成」会误导成仍会出图。"""
    assert ".echarts-container.echarts-failed::before" in _CARD_VIEWERS_SRC
    m = re.search(
        r"\.echarts-container\.echarts-failed::before \{\{(.*?)\}\}",
        _CARD_VIEWERS_SRC,
        re.S,
    )
    assert m and "加载失败" in m.group(1), "失败提示未区分「数据不全」与「资源加载失败」"


def test_echarts_ensure_bails_when_lib_failed():
    """库加载失败后再次遇到图表不应重新排队（CDN 已断，重试只会再空一帧）。"""
    m = re.search(
        r"function _echartsEnsure\(cb\) \{(.*?)\n                \}\}",
        _CARD_VIEWERS_SRC,
        re.S,
    )
    assert m, "未找到 _echartsEnsure 定义"
    assert "_echLibFailed" in m.group(1), "缺少失败熔断标记"


def test_skeleton_version_bumped_for_js_change():
    """改骨架 JS 必须递增 `_SKELETON_CACHE_VERSION`，否则复用旧骨架 JS 不生效。"""
    ver = card_render_core._SKELETON_CACHE_VERSION
    assert ver >= 77, f"骨架 JS 改了但版本号仍是 {ver}（旧缓存会让修复不生效）"


# ============ 三、打包清单：echarts vendor 必须进包 ============

@pytest.mark.parametrize(
    "vendor",
    ["echarts.min.js", "echarts-wordcloud.min.js"],
)
def test_build_includes_echarts_vendor(vendor):
    """macOS 打包版 vendor 目录实测缺这两个文件 → 只能走 CDN，网络一断全空。"""
    assert f"--add-data=app/resources/web/vendor/{vendor}" in _BUILD_SRC, (
        f"build.py 未把 {vendor} 打进包，打包版 echarts 依赖 CDN"
    )
