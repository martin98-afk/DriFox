# -*- coding: utf-8 -*-
"""按内容类型自动选渲染通道：`_content_needs_webengine` / `_should_use_qt_renderer`。

背景：纯 Qt 块级渲染器（MarkdownBlockViewer）单卡约 3 MB，QWebEngineView 约
40 MB（实测见 tests/perf/test_ui_memory_baseline.py），差约 13 倍。但纯 Qt 渲染
不了 echarts / mermaid / LaTeX / 图片 / 插件注册 fence，故不能全局切换 —— 只能
在**建 viewer 前预扫内容**决定通道。

核心契约：**保守优先**。任何不确定（拿不到插件注册表 / 内容不可解析 / 流式
期间内容不全）一律判定为「需要 WebEngine」。宁可少省内存，绝不让本该正常
显示的内容被降级。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets")

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import app.widgets.card_render_core as crc  # noqa: E402


# ── R2-A：判定函数 ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "这是一段普通中文正文，没有任何特殊语法。",
        "# 标题\n\n段落文本。",
        "- 列表项一\n- 列表项二",
        "| a | b |\n|---|---|\n| 1 | 2 |",
        "> 引用文本",
        "普通 `inline code` 与 `$HOME` 变量",
        "价格 100$ 元（单个美元符号，非公式）",
    ],
)
def test_plain_content_does_not_need_webengine(text):
    """纯文本 / 普通代码块 / 表格 / 列表 / 引用 → 纯 Qt 可胜任。"""
    assert crc._content_needs_webengine(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "```python\nprint('hello')\n```",
        "```js\nconst a = 1;\n```",
        "```bash\nls -la\n```",
        "```\n无 lang 的裸代码块\n```",
        "~~~python\nprint(1)\n~~~",
    ],
)
def test_ordinary_code_block_does_not_need_webengine(text):
    """普通代码块（Pygments 高亮，纯 Qt 支持）→ 不需要 WebEngine。"""
    assert crc._content_needs_webengine(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "```echarts\n{}\n```",
        "```mermaid\ngraph TD\n```",
        "```html\n<div>x</div>\n```",
        "```svg\n<svg/>\n```",
        "```widget\n{}\n```",
        "~~~echarts\n{}\n~~~",  # 波浪号 fence
        "```ECHARTS\n{}\n```",  # 大小写不敏感
        "  ```echarts\n{}\n```",  # 缩进 fence
        '```echarts\n{"a": 1',  # 未闭合 fence 也要判 True（保守）
    ],
)
def test_render_fence_needs_webengine(text):
    """渲染型 fence（内置集合）→ 必须 WebEngine。"""
    assert crc._content_needs_webengine(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "公式 $E=mc^2$ 在此",
        "$$\nx^2 + y^2\n$$",
        "$$x^2$$",
        "\\[ a + b \\]",
    ],
)
def test_latex_needs_webengine(text):
    """LaTeX 公式 → 必须 WebEngine（纯 Qt 无公式渲染）。"""
    assert crc._content_needs_webengine(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "![alt](http://example.com/a.png)",
        "![](/local/path/b.jpg)",
    ],
)
def test_markdown_image_needs_webengine(text):
    """markdown 正文图片 → 必须 WebEngine（纯 Qt 未实现图片异步加载）。"""
    assert crc._content_needs_webengine(text) is True


def test_none_content_is_conservative():
    """内容不可解析（None）→ 保守返回 True。"""
    assert crc._content_needs_webengine(None) is True


def test_unsupported_type_is_conservative():
    """非 str / list 类型 → 保守返回 True。"""
    assert crc._content_needs_webengine(12345) is True


def test_empty_content_does_not_need_webengine():
    """空内容没有可降级的东西 → False。"""
    assert crc._content_needs_webengine("") is False


def test_registry_unavailable_is_conservative(monkeypatch):
    """插件注册表拿不到（未加载 / 异常）→ 保守返回 True。

    这是硬契约：插件 fence 是动态注册的，预扫时 registry 可能尚未加载完，
    此时必须按「需要 WebEngine」处理，不能因为拿不到表就放行纯 Qt。
    """
    monkeypatch.setattr(crc, "_registered_fence_langs", lambda: None)
    # 即便是纯文本，拿不到插件表也必须保守
    assert crc._content_needs_webengine("普通正文") is True


def test_plugin_registered_fence_needs_webengine(monkeypatch):
    """插件注册的 fence lang（不在内置集合里）→ 必须 WebEngine。"""
    monkeypatch.setattr(crc, "_registered_fence_langs", lambda: frozenset({"myplugin"}))
    assert crc._content_needs_webengine("```myplugin\n{}\n```") is True
    # 未注册的 lang 仍按普通代码块处理
    assert crc._content_needs_webengine("```python\nprint(1)\n```") is False


# ── R2-B：通道决策 + 回退开关 ─────────────────────────────────────────
def _enable_auto(monkeypatch):
    """显式启用自动通道，并清掉可能残留的强制开关。

    ⚠️ 必须同时清 ``DRIFOX_QT_RENDERER``：tests/perf/test_ui_memory_baseline.py
    在模块收集期设了 ``os.environ["DRIFOX_QT_RENDERER"]="1"`` 且不清理，
    混跑（同一 pytest 进程里先跑 perf 再跑本文件）时该变量仍在，
    显式强制会盖过内容判定，导致本文件的分流断言失败。
    """
    monkeypatch.setenv(crc._RENDER_CHANNEL_AUTO_ENV, "1")
    monkeypatch.delenv("DRIFOX_QT_RENDERER", raising=False)


def test_channel_decision_plain_goes_qt(monkeypatch):
    """普通内容 → 纯 Qt（省内存）。仅在自动通道显式启用时成立。"""
    _enable_auto(monkeypatch)
    assert crc._should_use_qt_renderer("普通中文正文", streaming=False) is True


def test_channel_decision_chart_goes_webengine(monkeypatch):
    """图表内容 → WebEngine（功能不降级）。"""
    _enable_auto(monkeypatch)
    assert crc._should_use_qt_renderer("```echarts\n{}\n```", streaming=False) is False


def test_streaming_always_goes_webengine(monkeypatch):
    """⚠️ 流式期间内容不全 → 一律 WebEngine（启用态下）。

    这是本机制最大的风险点：echarts fence 可能在流式后段才到达，若在流式
    开始前按当时不完整的内容判为「不需要 WebEngine」，等 fence 到达时通道
    已定，图表会被永久降级。故流式期间强制走 WebEngine。
    """
    _enable_auto(monkeypatch)
    # 即使是纯文本，流式期间也必须走 WebEngine
    assert crc._should_use_qt_renderer("普通中文正文", streaming=True) is False


def test_auto_disabled_by_default(monkeypatch):
    """默认关闭自动通道（终态：用户否决画质妥协）。

    这是硬契约：未设置环境变量时，行为必须与改动前完全一致（全走 WebEngine）。
    """
    monkeypatch.delenv(crc._RENDER_CHANNEL_AUTO_ENV, raising=False)
    assert crc._auto_render_channel_enabled() is False


def test_default_state_equals_legacy_behavior(monkeypatch):
    """关闭态（默认）与原「全局二值开关」行为完全一致。

    原行为 = 只看 qt_message_renderer / DRIFOX_QT_RENDERER，默认 False →
    全部走 WebEngine。这是用户否决画质妥协后的终态。
    """
    monkeypatch.delenv(crc._RENDER_CHANNEL_AUTO_ENV, raising=False)
    monkeypatch.delenv("DRIFOX_QT_RENDERER", raising=False)
    assert crc._should_use_qt_renderer("普通中文正文", streaming=False) == crc._qt_renderer_enabled()
    assert crc._should_use_qt_renderer("普通中文正文", streaming=False) is False


def test_disabled_state_skips_content_judgment(monkeypatch):
    """关闭态下判定函数不被调用（零新增开销）。

    用户否决画质妥协后这是硬要求：默认路径上 `_content_needs_webengine` 一次
    都不该执行。
    """
    monkeypatch.delenv(crc._RENDER_CHANNEL_AUTO_ENV, raising=False)
    calls = []
    monkeypatch.setattr(crc, "_content_needs_webengine", lambda c: calls.append(c) or True)
    crc._should_use_qt_renderer("普通中文正文", streaming=False)
    crc._should_use_qt_renderer("```echarts\n{}\n```", streaming=False)
    crc._should_use_qt_renderer("普通中文正文", streaming=True)
    assert calls == [], f"关闭态下判定函数被调用了 {len(calls)} 次"


def test_enable_switch_variants(monkeypatch):
    """启用开关的多种写法都能识别（显式 opt-in）。"""
    for val in ("1", "true", "on", "yes", "TRUE", "On"):
        monkeypatch.setenv(crc._RENDER_CHANNEL_AUTO_ENV, val)
        assert crc._auto_render_channel_enabled() is True, f"{val} 未识别为启用"


def test_unset_and_other_values_disable(monkeypatch):
    """未设置 / 非启用值一律关闭（保守：opt-in 而非 opt-out）。"""
    for val in ("", "0", "false", "off", "no", "enabled", "auto"):
        monkeypatch.setenv(crc._RENDER_CHANNEL_AUTO_ENV, val)
        assert crc._auto_render_channel_enabled() is False, f"{val} 未识别为关闭"


def test_enabled_state_does_content_routing(monkeypatch):
    """启用态（opt-in）按内容分流：普通 → 纯 Qt，图表 → WebEngine。"""
    monkeypatch.setenv(crc._RENDER_CHANNEL_AUTO_ENV, "1")
    monkeypatch.delenv("DRIFOX_QT_RENDERER", raising=False)
    assert crc._should_use_qt_renderer("普通中文正文", streaming=False) is True
    assert crc._should_use_qt_renderer("```echarts\n{}\n```", streaming=False) is False


def test_legacy_switch_still_respected(monkeypatch):
    """DRIFOX_QT_RENDERER=1 的旧强制行为不变（显式开关优先于自动判定）。"""
    monkeypatch.delenv(crc._RENDER_CHANNEL_AUTO_ENV, raising=False)
    monkeypatch.setenv("DRIFOX_QT_RENDERER", "1")
    assert crc._should_use_qt_renderer("普通中文正文", streaming=False) is True


def test_explicit_force_beats_auto_decision(monkeypatch):
    """自动通道启用时，DRIFOX_QT_RENDERER=1 显式强制仍优先于内容判定。

    排障 / 测试场景靠这个开关强制纯 Qt；若被内容判定覆盖，开关会失效
    （实测曾导致 tests/perf/test_ui_memory_baseline.py 回落到 WebEngine 而崩，
    因为 offscreen 下 QWebEngineView 必然崩溃）。
    """
    monkeypatch.setenv(crc._RENDER_CHANNEL_AUTO_ENV, "1")
    monkeypatch.setenv("DRIFOX_QT_RENDERER", "1")
    # 图表内容、流式期间，显式强制都要走纯 Qt
    assert crc._should_use_qt_renderer("```echarts\n{}\n```", streaming=False) is True
    assert crc._should_use_qt_renderer("普通正文", streaming=True) is True
