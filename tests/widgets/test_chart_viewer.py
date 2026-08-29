# -*- coding: utf-8 -*-
"""图表查看器测试：HTML 模板、b64 往返、payload 上限、弹窗回退、白名单注册

运行: python -m pytest tests/widgets/test_chart_viewer.py -v
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
        """echarts 模式：含 vendor script、payload、导出入口、dark 主题"""
        html = build_chart_viewer_html("echarts", _b64('{"series": []}'))
        assert "echarts.min.js" in html
        assert "echarts.init" in html
        assert "'dark'" in html
        assert "window._exportChartPng" in html
        assert _b64('{"series": []}') in html  # payload 原样嵌入

    def test_mermaid_mode(self):
        """mermaid 模式：不引 echarts，SVG 注入 + 自适应 CSS + canvas 导出"""
        svg = '<svg width="800" height="600">'
        html = build_chart_viewer_html("mermaid", _b64(svg))
        assert "echarts.min.js" not in html
        assert "new Image" in html  # SVG → Image → canvas 导出链路
        assert "max-width: 100%" in html

    def test_payload_too_large_rejected(self):
        """payload 超 8MB 上限 → ValueError（防御，JS 侧也有同限拦截）"""
        big = "A" * (_MAX_PAYLOAD_B64 + 1)
        with pytest.raises(ValueError):
            build_chart_viewer_html("echarts", big)


class TestSavePngFromB64:
    def test_save_writes_file(self, tmp_path, monkeypatch):
        """弹窗路径选择后写出 PNG 字节（依赖 Task 2 新增 ui_helpers.save_png_from_b64）"""
        from app.widgets import ui_helpers as _uh

        if not hasattr(_uh, "save_png_from_b64"):
            pytest.skip("save_png_from_b64 由 Task 2 提供")

        from PySide6.QtWidgets import QFileDialog

        from app.widgets.ui_helpers import save_png_from_b64

        target = tmp_path / "out.png"
        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            staticmethod(lambda *a, **k: (str(target), "PNG 图片 (*.png)")),
        )
        raw = b"\x89PNG-fake-bytes"
        path = save_png_from_b64(None, base64.b64encode(raw).decode("ascii"), "测试图")
        assert path is not None and Path(path).read_bytes() == raw

    def test_cancel_returns_none(self, monkeypatch):
        """用户取消 → None，不写文件（依赖 Task 2 新增 ui_helpers.save_png_from_b64）"""
        from app.widgets import ui_helpers as _uh

        if not hasattr(_uh, "save_png_from_b64"):
            pytest.skip("save_png_from_b64 由 Task 2 提供")

        from PySide6.QtWidgets import QFileDialog

        from app.widgets.ui_helpers import save_png_from_b64

        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            staticmethod(lambda *a, **k: ("", "")),
        )
        assert save_png_from_b64(None, _b64("x"), "n") is None


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
