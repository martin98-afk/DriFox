# -*- coding: utf-8 -*-
"""图表查看器测试：HTML 模板、b64 往返、payload 上限、弹窗回退、白名单注册
运行: python -m pytest tests/widgets/test_chart_viewer.py -v

注意：与 tests 既有 stub 风格一致——不初始化真实 QWebEngineProfile/WebEngine
（offscreen + pytest 下 profile 创建会卡死），信号链/compose 均用骨架 + stub 验证
Python 侧解析与像素合成逻辑，WebEngine 运行时行为需人工/集成验证。
"""

import base64
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.widgets.cards.settings.chart_viewer_card import (
    _MAX_PAYLOAD_B64,
    build_chart_viewer_html,
    decode_chart_payload,
)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class TestDecodePayload:
    def test_utf8_roundtrip(self):
        """b64 → UTF-8 中文往返无损"""
        src = '{"title": {"text": "脱硝效率 DAG 图"}}'
        assert decode_chart_payload(_b64(src)) == src

    def test_invalid_b64_returns_empty(self):
        """非法 b64 → 空串（不抛异常，调用方安全降级）"""
        assert decode_chart_payload("!!!not-b64!!!") == ""


class TestBuildHtml:
    def test_echarts_mode(self):
        """echarts 模式：含 vendor script、payload、导出入口、dark 主题、dataZoom 缩放"""
        html = build_chart_viewer_html("echarts", _b64('{"series": []}'))
        assert "echarts.min.js" in html
        assert "echarts.init" in html
        assert "'dark'" in html
        assert "window._exportChartPng" in html
        assert "dataZoom" in html  # 滚轮缩放 + 底部滑条（局部放大）
        assert _b64('{"series": []}') in html  # payload 原样嵌入

    def test_mermaid_mode(self):
        """mermaid 模式：不引 echarts，SVG 注入 + 自适应 CSS + canvas 导出（无 dataZoom）"""
        svg = '<svg width="800" height="600">'
        html = build_chart_viewer_html("mermaid", _b64(svg))
        assert "echarts.min.js" not in html
        assert "new Image" in html  # SVG → Image → canvas 导出链路
        assert "max-width: 100%" in html
        assert "dataZoom" not in html  # dataZoom 是 echarts 专属，mermaid 不注入

    def test_payload_too_large_rejected(self):
        """payload 超 8MB 上限 → ValueError（防御，JS 侧也有同限拦截）"""
        big = "A" * (_MAX_PAYLOAD_B64 + 1)
        with pytest.raises(ValueError):
            build_chart_viewer_html("echarts", big)


class TestSavePngFromB64:
    def test_user_cancel_returns_none(self, qapp, monkeypatch):
        """用户取消保存 → None"""
        from PyQt5.QtWidgets import QFileDialog

        from app.widgets.ui_helpers import save_png_from_b64

        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            staticmethod(lambda *a, **k: ("", "")),
        )
        assert save_png_from_b64(None, _b64("x"), "n") is None

    def test_saves_and_appends_ext(self, qapp, monkeypatch, tmp_path):
        """正常保存 + 自动补 .png 后缀"""
        from PyQt5.QtWidgets import QFileDialog

        from app.widgets.ui_helpers import save_png_from_b64

        target = tmp_path / "out"
        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            staticmethod(lambda *a, **k: (str(target), "PNG 图片 (*.png)")),
        )
        import base64 as b64mod

        raw = b"\x89PNG fake"
        path = save_png_from_b64(None, base64.b64encode(raw).decode("ascii"), "n")
        assert path is not None
        assert path == str(tmp_path / "out.png")
        assert Path(path).read_bytes() == raw
        assert b64mod.b64encode(Path(path).read_bytes()).decode("ascii")


class TestRegistry:
    def test_replace_tab_bar_whitelist(self):
        """chart_viewer 进白名单 + 中文标题（tab 栏出现可关闭「图表查看」）"""
        from app.widgets.replace_tab_bar import GLOBAL_REPLACE_TITLES, KNOWN_GLOBAL_REPLACE_CARDS

        assert "chart_viewer" in KNOWN_GLOBAL_REPLACE_CARDS
        assert GLOBAL_REPLACE_TITLES.get("chart_viewer") == "图表查看"

    def test_ui_helpers_exports(self):
        """ui_helpers 导出新顶层函数"""
        from app.widgets import ui_helpers

        assert callable(ui_helpers.show_chart_viewer)
        assert callable(ui_helpers.save_png_from_b64)


class TestCardSkeletonHooks:
    def test_skeleton_contains_toolbar_js(self):
        """消息卡源码包含图表工具栏 JS 与导出/放大通道"""
        import inspect

        from app.widgets import message_card

        src = inspect.getsource(message_card)
        assert "_attachChartToolbar" in src
        assert "pywebview_action:chart_expand:" in src
        assert "pywebview_action:save_chart_png:" in src
        assert "_chartInstance" in src

    def test_capture_has_fallback_chain(self):
        """整卡导出含稳健回退链：3x 健康检查 → 1x 完整路径 → 裸 grab"""
        import inspect

        from app.widgets import message_card

        src = inspect.getsource(message_card)
        assert "_capture_looks_healthy" in src
        assert "_wait_render_stable" in src
        assert "_capture_full_content_1x" in src

    def test_skeleton_css_position_relative(self):
        """容器 CSS 含 position: relative（工具栏绝对定位前提）"""
        import inspect

        from app.widgets import message_card

        src = inspect.getsource(message_card)
        assert ".chart-toolbar" in src
        assert "position: relative" in src


class TestSignalChain:
    """console 消息解析 → 信号发射（stub 信号收集，不初始化真实 WebEngine）"""

    @staticmethod
    def _make_page_with_stub_signals():
        from app.widgets.message_card import ConsoleMonitorPage

        page = ConsoleMonitorPage.__new__(ConsoleMonitorPage)  # 跳过 __init__，不触碰 Qt WebEngine

        class _FakeSig:
            def __init__(self, sink):
                self._sink = sink

            def emit(self, *a):
                self._sink.append(a)

        expand_events = []
        png_events = []
        # PyQt5 信号是非数据描述符，实例属性赋值可安全遮蔽（仅测试期，对象不外泄）
        page.chartExpandRequested = _FakeSig(expand_events)  # type: ignore[assignment]
        page.saveChartPngRequested = _FakeSig(png_events)  # type: ignore[assignment]
        return page, expand_events, png_events

    def test_console_message_emits_chart_expand(self):
        """console 消息 chart_expand → ConsoleMonitorPage 信号 (type, payload)"""
        page, expand_events, _ = self._make_page_with_stub_signals()
        page.javaScriptConsoleMessage(0, "pywebview_action:chart_expand:echarts:eyJhIjoxfQ==", 0, "")  # type: ignore[arg-type]
        assert expand_events == [("echarts", "eyJhIjoxfQ==")]

    def test_console_message_emits_chart_expand_mermaid(self):
        """mermaid 类型同样放行"""
        page, expand_events, _ = self._make_page_with_stub_signals()
        page.javaScriptConsoleMessage(0, "pywebview_action:chart_expand:mermaid:PHN2Zz48L3N2Zz4=", 0, "")  # type: ignore[arg-type]
        assert expand_events == [("mermaid", "PHN2Zz48L3N2Zz4=")]

    def test_console_message_emits_save_png(self):
        """console 消息 save_chart_png → (name, png_b64) 信号"""
        page, _, png_events = self._make_page_with_stub_signals()
        page.javaScriptConsoleMessage(0, "pywebview_action:save_chart_png:aGVsbG8=:UENHXg==", 0, "")  # type: ignore[arg-type]
        assert png_events == [("aGVsbG8=", "UENHXg==")]  # page 层透传原始 b64，name 解码在 MessageCard 槽

    def test_oversize_payload_rejected(self):
        """超 8MB payload 拒绝发射"""
        from app.widgets.message_card import _MAX_CHART_PAYLOAD_B64

        page, expand_events, _ = self._make_page_with_stub_signals()
        payload = "pywebview_action:chart_expand:echarts:" + "A" * (_MAX_CHART_PAYLOAD_B64 + 1)
        page.javaScriptConsoleMessage(
            0,  # type: ignore[arg-type]
            payload,
            0,
            "",
        )
        assert expand_events == []

    def test_unknown_chart_type_rejected(self):
        """非 echarts/mermaid 类型拒绝发射"""
        page, expand_events, _ = self._make_page_with_stub_signals()
        page.javaScriptConsoleMessage(0, "pywebview_action:chart_expand:evil:AAAA", 0, "")  # type: ignore[arg-type]
        assert expand_events == []


class TestComposeWithDpr:
    """_compose_with_solid_bg 的 dpr 物理像素行为（__new__ 骨架 + stub 背景色，不初始化真实 WebEngine）"""

    @staticmethod
    def _make_viewer_with_stub_bg():
        from app.widgets.message_card import CodeWebViewer

        viewer = CodeWebViewer.__new__(CodeWebViewer)
        from PyQt5.QtGui import QColor

        viewer._get_card_bg_color = lambda: QColor("#2B2B2B")  # stub：沿父链找卡色需完整控件树
        return viewer

    def test_compose_scales_physical_pixels(self, qapp):
        """compose 带 dpr=3 → 输出物理像素 3x、逻辑尺寸还原"""
        from PyQt5.QtGui import QPixmap

        viewer = self._make_viewer_with_stub_bg()
        src = QPixmap(30, 20)
        out = viewer._compose_with_solid_bg(src, 100, 50, dpr=3.0)
        assert out.width() == 300 and out.height() == 150
        assert abs(out.devicePixelRatio() - 3.0) < 1e-6
        assert out.width() / out.devicePixelRatio() == 100  # 逻辑宽还原

    def test_compose_default_dpr_unchanged(self, qapp):
        """dpr 缺省 1.0 行为与旧版一致"""
        from PyQt5.QtGui import QPixmap

        viewer = self._make_viewer_with_stub_bg()
        out = viewer._compose_with_solid_bg(QPixmap(), 80, 40)
        assert out.width() == 80 and out.height() == 40

    def test_compose_dpr_below_one_clamped(self, qapp):
        """dpr<1 被钳制为 1.0（防止导出反而降采样）"""
        from PyQt5.QtGui import QPixmap

        viewer = self._make_viewer_with_stub_bg()
        out = viewer._compose_with_solid_bg(QPixmap(), 60, 30, dpr=0.5)
        assert out.width() == 60 and out.height() == 30
        assert abs(out.devicePixelRatio() - 1.0) < 1e-6
