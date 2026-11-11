# -*- coding: utf-8 -*-
"""SceneLayer 前景图层（foreground）回归测试

背景：壁纸拆出的全画布前景层（人物等）必须与 scene.image 共用同一矩形、
同一缩放方式（setScaledContents 拉伸铺满），才能与背景逐像素对齐。
历史方案用 decorations 摆放，reference 是对话滚动区且等比 fit，
同一张壁纸的两层构图必然错位（用户侧：「人物和背景对不上」）。
"""

import pytest

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QLabel, QWidget


def _write_png(path, w, h, color):
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(color)
    assert img.save(str(path))


@pytest.fixture()
def layer(qapp, tmp_path):
    from app.widgets.scene_layer import SceneLayer

    host = QWidget()
    host.resize(800, 600)
    lay = SceneLayer(host)
    lay.setGeometry(host.rect())
    bg = tmp_path / "scene.png"
    fg = tmp_path / "fg.png"
    _write_png(bg, 1600, 727, 0xFFFF0000)
    _write_png(fg, 1600, 727, 0x8000FF00)
    yield host, lay, str(bg), str(fg)
    host.deleteLater()


def _apply(lay, bg, fg, dim=None):
    cfg = {"image": bg, "opacity": 1.0, "enabled": True}
    if fg is not None:
        cfg["foreground"] = {"image": fg, "opacity": 0.55, "enabled": True}
    if dim is not None:
        cfg["dim"] = dim
    lay.apply_config(cfg, image_resolver=lambda p: p)


class TestForegroundGeometry:
    def test_foreground_matches_image_geometry(self, layer):
        """前景层几何必须与背景图完全一致（同矩形同拉伸）"""
        _host, lay, bg, fg = layer
        _apply(lay, bg, fg)
        assert lay._foreground_label is not None, "未创建前景层"
        assert lay._foreground_label.geometry() == lay._image_label.geometry()

    def test_foreground_follows_resize(self, layer):
        """宿主 resize 后前景层跟随，仍与背景同几何"""
        host, lay, bg, fg = layer
        _apply(lay, bg, fg)
        # 隐藏控件不派发 resizeEvent（Qt 延迟到 show），显式走一次几何同步
        # —— resizeEvent 内部就是调它
        lay.resize(1000, 700)
        lay._sync_content_geometry()
        assert lay._foreground_label.geometry() == lay._image_label.geometry()
        assert lay._foreground_label.width() == 1000

    def test_foreground_scaled_contents(self, layer):
        """前景层必须拉伸铺满（与 scene.image 相同的缩放规则）"""
        _host, lay, bg, fg = layer
        _apply(lay, bg, fg)
        assert lay._foreground_label.hasScaledContents() is True
        assert lay._image_label.hasScaledContents() is True


class TestForegroundZOrder:
    def test_foreground_above_dim(self, layer):
        """Z 序：image < dim < foreground（前景不被压暗蒙版盖住）"""
        _host, lay, bg, fg = layer
        _apply(lay, bg, fg, dim="rgba(0,0,0,0.4)")
        labels = [c for c in lay.children() if isinstance(c, QLabel)]
        order = [lay._image_label, lay._dim_label, lay._foreground_label]
        idx = [labels.index(w) for w in order]
        assert idx == sorted(idx), f"Z 序错误：image/dim/foreground 索引={idx}"


class TestForegroundLifecycle:
    def test_clear_removes_foreground(self, layer):
        """重应用或清空配置后前景层被清理"""
        _host, lay, bg, fg = layer
        _apply(lay, bg, fg)
        old = lay._foreground_label
        _apply(lay, bg, None)
        assert old.isHidden(), "旧前景层未立即隐藏"
        assert lay._foreground_label is None

    def test_foreground_disabled_ignored(self, layer):
        """foreground.enabled=False 时不创建"""
        _host, lay, bg, fg = layer
        lay.apply_config(
            {
                "image": bg,
                "enabled": True,
                "foreground": {"image": fg, "enabled": False},
            },
            image_resolver=lambda p: p,
        )
        assert lay._foreground_label is None


class TestThemeManagerNormalization:
    def test_foreground_survives_normalization(self, monkeypatch):
        """theme_manager 归一化不能把嵌套的 foreground 拍平丢掉"""
        from app.utils import theme_manager as tm

        monkeypatch.setitem(
            tm.theme_manager._themes,
            "__fg_test__",
            {
                "backgrounds": {
                    "scene": {
                        "image": "bg.jpg",
                        "dim": "rgba(0,0,0,0.4)",
                        "foreground": {"image": "fg.png", "opacity": 0.55},
                    }
                }
            },
        )
        bgs = tm.theme_manager.get_theme_backgrounds("__fg_test__")
        fg = bgs["scene"]["foreground"]
        assert fg is not None
        assert fg["image"] == "fg.png"
        assert fg["opacity"] == 0.55
        assert fg["enabled"] is True
