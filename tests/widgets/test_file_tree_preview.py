# -*- coding: utf-8 -*-
"""file-tree 插件：图片 / 文本预览回归测试

覆盖：
- 类型判定（图片按 Qt 实际支持格式、文本按后缀白名单、未知返回空）
- 文本限流（>2000 行截断并给出提示；二进制内容拒读）
- 编码兜底（GBK 中文正确解码，不抛）
- 图片限流（超大像素按长边缩放载入，且不超过上限）
- 预览页状态（载入 / 刷新 / 返回信号 / 缩放与平移钳制）
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

# QWidget 构造前必须存在 QApplication
_QAPP = QApplication.instance() or QApplication(sys.argv)

_PLUGIN_UI = Path(__file__).resolve().parent.parent.parent / "plugins" / "file-tree" / "ui"


def _load_package():
    if "file_tree_preview_ui" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "file_tree_preview_ui", _PLUGIN_UI / "__init__.py", submodule_search_locations=[str(_PLUGIN_UI)]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["file_tree_preview_ui"] = pkg
    spec.loader.exec_module(pkg)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load_package()
preview_mod = _load_module("file_tree_preview_ui.preview", _PLUGIN_UI / "preview.py")

detect_kind = preview_mod.detect_kind
read_text = preview_mod.read_text
read_image = preview_mod.read_image
FilePreviewPage = preview_mod.FilePreviewPage


# ── 类型判定 ──


class TestDetectKind:
    @pytest.mark.parametrize("name", ["a.png", "shot.JPG", "x.bmp", "y.webp"])
    def test_image_extensions(self, tmp_path, name):
        p = tmp_path / name
        p.write_bytes(b"x")
        assert detect_kind(str(p)) == "image"

    @pytest.mark.parametrize("name", ["a.py", "b.json", "c.md", "d.log", "e.yaml", "f.csv", "g.sh"])
    def test_text_extensions(self, tmp_path, name):
        p = tmp_path / name
        p.write_text("x", encoding="utf-8")
        assert detect_kind(str(p)) == "text"

    @pytest.mark.parametrize("name", ["Dockerfile", "Makefile", "LICENSE"])
    def test_text_by_bare_name(self, tmp_path, name):
        p = tmp_path / name
        p.write_text("x", encoding="utf-8")
        assert detect_kind(str(p)) == "text"

    # 注意不要把 pdf 列入：PySide6 的 QImageReader 自带 pdf 插件（Qt6 新增），
    # detect_kind 按 Qt 实际能力判定，会正确返回 image（PyQt5 版不支持）。
    @pytest.mark.parametrize("name", ["a.exe", "c.mp4", "d.bin"])
    def test_unsupported(self, tmp_path, name):
        p = tmp_path / name
        p.write_bytes(b"x")
        assert detect_kind(str(p)) == ""

    def test_missing_file(self, tmp_path):
        assert detect_kind(str(tmp_path / "nope.txt")) == ""
        assert detect_kind("") == ""

    def test_directory_is_not_previewable(self, tmp_path):
        assert detect_kind(str(tmp_path)) == ""


# ── 文本读取 ──


class TestReadText:
    def test_small_file_no_hint(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("line1\nline2\n", encoding="utf-8")
        text, hint = read_text(str(p))
        assert "line1" in text and "line2" in text
        assert hint == ""

    def test_truncates_over_line_cap(self, tmp_path):
        p = tmp_path / "big.log"
        cap = preview_mod.TEXT_MAX_LINES
        p.write_text("\n".join(f"l{i}" for i in range(cap + 500)), encoding="utf-8")
        text, hint = read_text(str(p))
        assert len(text.splitlines()) == cap
        assert str(cap) in hint

    def test_binary_rejected(self, tmp_path):
        p = tmp_path / "weird.txt"
        p.write_bytes(b"abc\x00def")
        with pytest.raises(ValueError):
            read_text(str(p))

    def test_gbk_fallback(self, tmp_path):
        p = tmp_path / "gbk.txt"
        p.write_bytes("中文内容测试".encode("gbk"))
        text, _ = read_text(str(p))
        assert text == "中文内容测试"

    def test_utf8_bom_stripped(self, tmp_path):
        p = tmp_path / "bom.txt"
        p.write_bytes("\ufeffhello".encode("utf-8"))
        text, _ = read_text(str(p))
        assert text == "hello"


# ── 图片读取 ──


def _write_png(path, w, h):
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor("#3366cc"))
    assert img.save(str(path), "PNG")
    return path


class TestReadImage:
    def test_small_image_loads_native(self, tmp_path):
        p = _write_png(tmp_path / "s.png", 40, 30)
        pixmap, hint = read_image(str(p))
        assert (pixmap.width(), pixmap.height()) == (40, 30)
        assert hint == ""

    def test_huge_image_downscaled(self, tmp_path, monkeypatch):
        # 缩小阈值制造「超大像素」场景，避免真的生成 2000 万像素图
        monkeypatch.setattr(preview_mod, "IMAGE_MAX_PIXELS", 100)
        monkeypatch.setattr(preview_mod, "IMAGE_MAX_SIDE", 20)
        p = _write_png(tmp_path / "big.png", 60, 40)
        pixmap, hint = read_image(str(p))
        assert max(pixmap.width(), pixmap.height()) <= 20
        assert "已缩小载入" in hint

    def test_oversize_bytes_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(preview_mod, "IMAGE_MAX_BYTES", 8)
        p = _write_png(tmp_path / "o.png", 20, 20)
        with pytest.raises(ValueError):
            read_image(str(p))

    def test_corrupt_image_rejected(self, tmp_path):
        p = tmp_path / "bad.png"
        p.write_bytes(b"not a png at all")
        with pytest.raises(ValueError):
            read_image(str(p))


# ── 预览页 ──


def _colors():
    return {
        "accent": QColor("#62a0ea"),
        "border": QColor(255, 255, 255, 30),
        "text": QColor(230, 230, 230),
        "text_secondary": QColor(150, 150, 150),
        "card_bg": QColor(33, 33, 38),
        "is_dark": True,
        "font_family": "Microsoft YaHei",
        "font_size": 14,
    }


class TestPreviewPage:
    def test_text_load_shows_code_view(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_text("print(1)\nprint(2)\n", encoding="utf-8")
        page = FilePreviewPage()
        try:
            page.set_colors(_colors())
            page.load(str(p))
            assert page.current_kind() == "text"
            assert page._content.currentIndex() == 1
            assert "print(2)" in page._code_view.toPlainText()
            assert page._name_label.full_text() == "a.py"
        finally:
            page.deleteLater()

    def test_image_load_shows_image_view(self, tmp_path):
        p = _write_png(tmp_path / "s.png", 30, 20)
        page = FilePreviewPage()
        try:
            page.set_colors(_colors())
            page.load(str(p))
            assert page.current_kind() == "image"
            assert page._content.currentIndex() == 0
            assert page._image_view.has_image()
            assert page._zoom_in_btn.isVisibleTo(page)
        finally:
            page.deleteLater()

    def test_unsupported_shows_message(self, tmp_path):
        p = tmp_path / "a.exe"
        p.write_bytes(b"MZ")
        page = FilePreviewPage()
        try:
            page.set_colors(_colors())
            page.load(str(p))
            assert page.current_kind() == ""
            assert page._content.currentIndex() == 2
            assert "不支持预览" in page._msg_label.text()
        finally:
            page.deleteLater()

    def test_binary_text_shows_error_message(self, tmp_path):
        p = tmp_path / "x.txt"
        p.write_bytes(b"abc\x00def")
        page = FilePreviewPage()
        try:
            page.set_colors(_colors())
            page.load(str(p))
            assert page._content.currentIndex() == 2
            assert "二进制" in page._msg_label.text()
        finally:
            page.deleteLater()

    def test_reload_current_rereads(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("v1", encoding="utf-8")
        page = FilePreviewPage()
        try:
            page.set_colors(_colors())
            page.load(str(p))
            p.write_text("v2", encoding="utf-8")
            assert page.reload_current() is True
            assert page._code_view.toPlainText() == "v2"
            p.unlink()
            assert page.reload_current() is False
        finally:
            page.deleteLater()

    def test_back_button_emits(self, tmp_path):
        page = FilePreviewPage()
        seen = []
        try:
            page.back_requested.connect(lambda: seen.append(1))
            page._back_btn.click()
            assert seen
        finally:
            page.deleteLater()


class TestImageView:
    def _view(self, w=100, h=80, img_w=200, img_h=160):
        view = preview_mod._ImageView()
        view.resize(w, h)
        img = QImage(img_w, img_h, QImage.Format_RGB32)
        img.fill(QColor("#3366cc"))
        from PySide6.QtGui import QPixmap

        view.set_pixmap(QPixmap.fromImage(img))
        return view

    def test_fit_scale_never_upscales(self):
        view = self._view(w=800, h=800, img_w=100, img_h=100)
        try:
            assert view._fit_scale() == pytest.approx(1.0)
        finally:
            view.deleteLater()

    def test_zoom_clamped_to_range(self):
        view = self._view()
        try:
            for _ in range(60):
                view.zoom_in()
            assert view.zoom <= view.MAX_ZOOM
            for _ in range(200):
                view.zoom_out()
            assert view.zoom >= view.MIN_ZOOM
        finally:
            view.deleteLater()

    def test_reset_zoom_centers(self):
        view = self._view()
        try:
            view.zoom_in()
            view._offset = preview_mod.QPointF(30.0, 30.0)
            view.reset_zoom()
            assert view.zoom == pytest.approx(1.0)
            assert view._offset.x() == 0.0 and view._offset.y() == 0.0
        finally:
            view.deleteLater()

    def test_offset_clamped_when_image_smaller_than_view(self):
        view = self._view(w=400, h=400, img_w=50, img_h=50)
        try:
            view._offset = preview_mod.QPointF(999.0, -999.0)
            view._clamp_offset()
            assert view._offset.x() == 0.0 and view._offset.y() == 0.0
        finally:
            view.deleteLater()
