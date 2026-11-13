# -*- coding: utf-8 -*-
"""file-tree 插件：预览增强回归测试（语法高亮 / 自动换行 / 打开方式）

覆盖：
- 语法高亮分桶（样式键命中、行号对齐、未知类型与超大文件降级）
- 代码视图开关（高亮关闭后 spans 清空、换行状态可切、字号可缩放）
- 打开方式（命令行占位符实例化、无占位符追加路径、非 Windows 空列表）
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from PyQt5.QtGui import QColor, QImage
from PyQt5.QtWidgets import QApplication

_QAPP = QApplication.instance() or QApplication(sys.argv)

_PLUGIN_UI = Path(__file__).resolve().parent.parent.parent / "plugins" / "file-tree" / "ui"


def _load_package():
    if "file_tree_extras_ui" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "file_tree_extras_ui", _PLUGIN_UI / "__init__.py", submodule_search_locations=[str(_PLUGIN_UI)]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["file_tree_extras_ui"] = pkg
    spec.loader.exec_module(pkg)


_load_package()
import file_tree_extras_ui.highlight as highlight_mod  # noqa: E402
import file_tree_extras_ui.openwith as openwith_mod  # noqa: E402
import file_tree_extras_ui.preview as preview_mod  # noqa: E402

_HAS_PYGMENTS = importlib.util.find_spec("pygments") is not None


# ── 语法高亮 ──

class TestHighlightSpans:
    @pytest.mark.skipif(not _HAS_PYGMENTS, reason="环境无 pygments")
    def test_python_styles_and_lines(self, tmp_path):
        src = 'import os\n\n# 注释\ndef run():\n    return "文本"\n'
        p = tmp_path / "a.py"
        p.write_text(src, encoding="utf-8")
        spans, language, skipped = highlight_mod.analyze(str(p), src)
        assert skipped == ""
        assert "Python" in language
        assert len(spans) == src.count("\n") + 1
        styles = {style for line in spans for _s, _e, style in line}
        assert {"keyword", "comment", "string"} <= styles
        # 第一行 "import" 命中关键字，且列范围落在 import 内
        import_spans = [s for s in spans[0] if s[2] == "keyword"]
        assert import_spans and import_spans[0][0] == 0 and import_spans[0][1] == len("import")

    def test_unknown_extension_degrades(self, tmp_path):
        p = tmp_path / "a.unknownext"
        text = "hello\nworld\n"
        p.write_text(text, encoding="utf-8")
        spans, language, skipped = highlight_mod.analyze(str(p), text)
        assert spans == [] and language == ""
        assert skipped in {"unknown", "failed"}

    @pytest.mark.skipif(not _HAS_PYGMENTS, reason="环境无 pygments")
    def test_oversize_bytes_degrades(self, tmp_path):
        p = tmp_path / "big.py"
        text = "x = 1\n" * 10
        p.write_text(text, encoding="utf-8")
        orig = highlight_mod.HIGHLIGHT_MAX_BYTES
        highlight_mod.HIGHLIGHT_MAX_BYTES = 4
        try:
            _spans, _lang, skipped = highlight_mod.analyze(str(p), text)
        finally:
            highlight_mod.HIGHLIGHT_MAX_BYTES = orig
        assert skipped == "too_large"

    @pytest.mark.skipif(not _HAS_PYGMENTS, reason="环境无 pygments")
    def test_oversize_lines_degrades(self, tmp_path):
        p = tmp_path / "long.py"
        text = "x = 1\n" * 10
        p.write_text(text, encoding="utf-8")
        orig = highlight_mod.HIGHLIGHT_MAX_LINES
        highlight_mod.HIGHLIGHT_MAX_LINES = 3
        try:
            _spans, _lang, skipped = highlight_mod.analyze(str(p), text)
        finally:
            highlight_mod.HIGHLIGHT_MAX_LINES = orig
        assert skipped == "too_many_lines"


# ── 代码视图开关 ──

class TestCodeViewToggles:
    def _view(self):
        view = preview_mod._CodeView()
        view.apply_theme(
            text_color=QColor(230, 230, 230),
            gutter_text=QColor(150, 150, 150),
            gutter_bg=QColor(255, 255, 255, 16),
            selection=QColor(80, 150, 220, 70),
            font_pixel_size=13,
            is_dark=True,
        )
        return view

    @pytest.mark.skipif(not _HAS_PYGMENTS, reason="环境无 pygments")
    def test_set_source_highlights_and_toggle_clears(self, tmp_path):
        p = tmp_path / "b.py"
        text = "def f():\n    return 1\n"
        p.write_text(text, encoding="utf-8")
        view = self._view()
        try:
            _lang, skipped = view.set_source(str(p), text)
            assert skipped == ""
            assert view._highlighter.has_spans()
            view.set_highlight_enabled(False)
            assert not view._highlighter.has_spans()
            view.set_highlight_enabled(True)
            assert view._highlighter.has_spans()
        finally:
            view.deleteLater()

    def test_wrap_toggle(self):
        view = self._view()
        try:
            assert view.wrap_enabled() is False
            view.set_wrap(True)
            assert view.wrap_enabled() is True
            view.set_wrap(False)
            assert view.wrap_enabled() is False
        finally:
            view.deleteLater()

    def test_font_size_clamped(self):
        view = self._view()
        try:
            view.set_font_pixel_size(100)
            assert view.font_pixel_size() == 32
            view.set_font_pixel_size(1)
            assert view.font_pixel_size() == 10
        finally:
            view.deleteLater()


# ── 预览页控件可见性 ──

class TestPreviewControls:
    def _page(self):
        page = preview_mod.FilePreviewPage()
        page.set_colors(
            {
                "text": QColor(230, 230, 230),
                "text_secondary": QColor(150, 150, 150),
                "accent": QColor(80, 150, 220),
                "border": QColor(255, 255, 255, 40),
                "is_dark": True,
                "font_family": "Microsoft YaHei",
                "font_size": 14,
            }
        )
        return page

    def test_text_shows_view_controls(self, tmp_path):
        p = tmp_path / "c.txt"
        p.write_text("hello\n", encoding="utf-8")
        page = self._page()
        try:
            assert page.load(str(p))
            assert page._wrap_btn.isVisible() or not page.isVisible()  # 未 show 时 isVisible 为 False
            assert page._wrap_btn.isHidden() is False
            assert page._highlight_btn.isHidden() is False
        finally:
            page.deleteLater()

    def test_image_hides_text_controls(self, tmp_path):
        p = tmp_path / "d.png"
        img = QImage(60, 40, QImage.Format_RGB32)
        img.fill(QColor("#3366cc"))
        assert img.save(str(p), "PNG")
        page = self._page()
        try:
            assert page.load(str(p))
            assert page._wrap_btn.isHidden() is True
            assert page._highlight_btn.isHidden() is True
            assert page._zoom_in_btn.isHidden() is False
        finally:
            page.deleteLater()

    def test_open_with_signal_emits_path(self, tmp_path):
        p = tmp_path / "e.txt"
        p.write_text("x\n", encoding="utf-8")
        page = self._page()
        got = []
        page.open_with_requested.connect(got.append)
        try:
            page.load(str(p))
            page._emit_open_with()
            assert got == [str(p)]
        finally:
            page.deleteLater()


# ── 打开方式 ──

class TestOpenWith:
    def test_build_args_replaces_placeholder(self):
        opener = openwith_mod.Opener(label="X", exe="C:/x/x.exe", command='"C:/x/x.exe" "%1"')
        assert openwith_mod.build_args(opener, "D:/a b/c.py") == ["C:/x/x.exe", "D:/a b/c.py"]

    def test_build_args_appends_when_no_placeholder(self):
        opener = openwith_mod.Opener(label="X", exe="C:/x/x.exe", command="C:/x/x.exe")
        assert openwith_mod.build_args(opener, "D:/c.py") == ["C:/x/x.exe", "D:/c.py"]

    def test_build_args_without_command(self):
        opener = openwith_mod.Opener(label="X", exe="C:/x/x.exe")
        assert openwith_mod.build_args(opener, "D:/c.py") == ["C:/x/x.exe", "D:/c.py"]

    def test_list_openers_smoke(self, tmp_path):
        p = tmp_path / "f.py"
        p.write_text("x\n", encoding="utf-8")
        openers = openwith_mod.list_openers(str(p))
        assert isinstance(openers, list)
        if os.name != "nt":
            assert openers == []
            return
        # Windows 上 python 文件一般至少有一个关联程序；字段完整性单独校验
        for opener in openers:
            assert opener.label
            assert opener.exe
            assert opener.command

    def test_list_openers_unknown_extension_empty(self, tmp_path):
        p = tmp_path / "g.zzzunknown"
        p.write_text("x\n", encoding="utf-8")
        assert openwith_mod.list_openers(str(p)) == []
