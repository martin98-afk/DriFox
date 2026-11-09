# -*- coding: utf-8 -*-
"""SceneLayer 摘 effect / GL 承载 / blur 烘焙 优化测试（2026-11-09 PAINT-PROF 驱动）

背景：动画主题背景 12fps 全窗 update，raster backing store 上层全部可见
控件连带重绘（实测峰值 3353 paint/秒，阈值 120）。本组测试锁定三个优化的
行为契约：

1. 透明度传导：整体 opacity 不再挂 QGraphicsOpacityEffect，改传导给
   QSS alpha 与各图层预乘 alpha；
2. blur 烘焙：静态图 blur 加载时离屏烘焙一次，不挂常驻 effect，
   resize 后防抖重烘；
3. GL 承载与回退：动画层 GL 上下文运行期失效时原地重建 raster 版，
   订阅与帧不丢。
"""

import pytest
from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QColor, QImage, QPainter, QPixmap, QResizeEvent
from PyQt5.QtWidgets import QApplication, QWidget

from app.widgets.scene_layer import (
    _AnimView,
    _premultiply_opacity,
    _scale_qss_color_alpha,
)


class _FakeSource:
    def __init__(self):
        self.failed = False
        self.fps = 12
        self.current = QImage(4, 4, QImage.Format_ARGB32)
        self.subs = []

    def subscribe(self, layer):
        if layer not in self.subs:
            self.subs.append(layer)

    def unsubscribe(self, layer):
        self.subs = [s for s in self.subs if s is not layer]

    def has_subscribers(self):
        return bool(self.subs)


class _FakeRegistry:
    def __init__(self, source):
        self.source = source

    def acquire(self, path, fps=12):
        return self.source


class _FakeClock:
    def __init__(self):
        self.tracked = []

    def track(self, source):
        self.tracked.append(source)


@pytest.fixture()
def harness(qapp, monkeypatch):
    from app.widgets import scene_layer as sl

    source = _FakeSource()
    clock = _FakeClock()
    monkeypatch.setattr(
        sl.AnimationRegistry, "instance", classmethod(lambda cls: _FakeRegistry(source))
    )
    monkeypatch.setattr(sl.BackgroundClock, "instance", classmethod(lambda cls: clock))

    host = QWidget()
    host.resize(400, 300)
    layer = sl.SceneLayer(host)
    layer.setGeometry(host.rect())
    yield host, layer, source, clock
    layer.clear()
    host.deleteLater()


def _paint_one_frame(view) -> QImage:
    """离屏 render 一帧（raster 路径烟测：paintEvent 全路径执行过才算验证）"""
    img = QImage(view.width() or 64, view.height() or 64, QImage.Format_ARGB32_Premultiplied)
    img.fill(0)
    painter = QPainter(img)
    view.render(painter)
    painter.end()
    return img


class TestOpacityConduction:
    def test_layer_opacity_conducted_no_effect(self, harness):
        """整体 opacity 传导后：动画层拿到 painter opacity，层自身无 effect"""
        host, layer, source, _clock = harness
        layer.apply_config(
            {"animation": {"src": "a.webp", "fps": 12}, "opacity": 0.8},
            image_resolver=lambda p: p,
        )
        assert layer._anim_view is not None
        assert layer._anim_view._opacity == pytest.approx(0.8)
        assert layer.graphicsEffect() is None, "SceneLayer 不得再挂常驻 effect"

    @staticmethod
    def _write_png(tmp_path, name="fg.png", w=64, h=48):
        path = tmp_path / name
        img = QImage(w, h, QImage.Format_ARGB32)
        img.fill(0xFF336699)
        assert img.save(str(path))
        return str(path)

    def test_fg_opacity_premultiplied(self, harness, tmp_path):
        """前景层 opacity × 整体 opacity 烘进 pixmap alpha（无 effect）"""
        host, layer, source, _clock = harness
        p = self._write_png(tmp_path)
        layer.apply_config(
            {
                "foreground": {"image": p, "opacity": 0.5},
                "opacity": 0.8,
            },
            image_resolver=lambda p_: p,
        )
        assert layer._foreground_label is not None
        assert layer._foreground_label.graphicsEffect() is None
        pm = layer._foreground_label.pixmap()
        assert pm is not None
        # 烘进 alpha 后任一非透明像素的 alpha 应 ≤ 0.5*0.8=0.4 对应值 102
        img = pm.toImage()
        max_alpha = 0
        for y in range(0, img.height(), max(1, img.height() // 8)):
            for x in range(0, img.width(), max(1, img.width() // 8)):
                max_alpha = max(max_alpha, (img.pixel(x, y) >> 24) & 0xFF)
        assert max_alpha <= 102

    def test_image_opacity_premultiplied(self, harness, tmp_path):
        host, layer, source, _clock = harness
        p = self._write_png(tmp_path, name="bg.png")
        layer.apply_config(
            {"image": p, "opacity": 0.5}, image_resolver=lambda p_: p
        )
        assert layer._image_label is not None
        assert layer._image_label.graphicsEffect() is None
        pm = layer._image_label.pixmap()
        assert pm is not None
        img = pm.toImage()
        max_alpha = 0
        for y in range(0, img.height(), max(1, img.height() // 8)):
            for x in range(0, img.width(), max(1, img.width() // 8)):
                max_alpha = max(max_alpha, (img.pixel(x, y) >> 24) & 0xFF)
        assert max_alpha <= 128

    def test_dim_alpha_scaled(self, harness):
        """dim 蒙版 QSS alpha 随整体 opacity 缩放"""
        host, layer, source, _clock = harness
        layer.apply_config(
            {"dim": "rgba(0, 0, 0, 200)", "opacity": 0.5},
            image_resolver=lambda p: p,
        )
        qss = layer._dim_label.styleSheet()
        color = qss.split("background-color:")[1].split(";")[0].strip()
        assert QColor(color).alpha() == 100


class TestPremultiplyHelper:
    def test_premultiply_scales_alpha(self):
        img = QImage(8, 8, QImage.Format_ARGB32)
        img.fill(0xFF336699)
        out = _premultiply_opacity(img, 0.5)
        # QRgb 取 alpha 用位运算（QColor(int) 重载歧义不可靠）
        assert ((out.pixel(0, 0) >> 24) & 0xFF) == 128

    def test_premultiply_noop_at_full(self):
        img = QImage(8, 8, QImage.Format_ARGB32)
        img.fill(0xFF336699)
        out = _premultiply_opacity(img, 1.0)
        assert out is img

    def test_scale_qss_color_alpha(self):
        out = _scale_qss_color_alpha("rgba(10, 20, 30, 200)", 0.5)
        c = QColor(out)
        assert c.alpha() == 100
        assert (c.red(), c.green(), c.blue()) == (10, 20, 30)
        # hex 输入同样传导（#RRGGBB 无 alpha 默认 255，255×0.25=64）
        c2 = QColor(_scale_qss_color_alpha("#112233", 0.25))
        assert c2.alpha() == 64
        # factor>=1 原样返回；非法色原样返回
        assert _scale_qss_color_alpha("#112233", 1.0) == "#112233"
        assert _scale_qss_color_alpha("not-a-color", 0.5) == "not-a-color"

    def test_parse_css_color_variants(self):
        from app.widgets.scene_layer import _parse_css_color

        # Qt rgba 风格（0-255 alpha）
        assert _parse_css_color("rgba(240, 246, 247, 40)").alpha() == 40
        # CSS3 风格（0~1 小数 alpha）
        assert _parse_css_color("rgba(0, 0, 0, 0.5)").alpha() == 128
        # rgb() 无 alpha
        assert _parse_css_color("rgb(1, 2, 3)").alpha() == 255


class TestBlurBake:
    def _write_png(self, tmp_path, name="bg.png", w=64, h=48):
        path = tmp_path / name
        img = QImage(w, h, QImage.Format_ARGB32)
        img.fill(0xFF336699)
        assert img.save(str(path))
        return str(path)

    def test_blur_baked_no_effect(self, harness, tmp_path):
        """blur 静态图：加载后烘焙，无常驻 effect，1:1 显示烘焙结果"""
        host, layer, source, _clock = harness
        p = self._write_png(tmp_path)
        layer.apply_config({"image": p, "blur": 8}, image_resolver=lambda p_: p)
        label = layer._image_label
        assert label is not None
        assert label.graphicsEffect() is None, "blur 不得再挂常驻 effect"
        assert layer._image_blur_radius == 8.0
        assert layer._blur_pad == 16
        pm = label.pixmap()
        assert pm is not None
        assert pm.width() == label.width() and pm.height() == label.height()
        assert label.hasScaledContents() is False, "烘焙后应 1:1 显示"
        assert layer._image_bake_key is not None

    def test_resize_rebakes(self, harness, tmp_path):
        """resize 后防抖重烘：烘焙 key 更新、尺寸跟随新几何"""
        host, layer, source, _clock = harness
        p = self._write_png(tmp_path)
        layer.apply_config({"image": p, "blur": 8}, image_resolver=lambda p_: p)
        old_key = layer._image_bake_key
        # resize 同步链：宿主装 filter → 层跟随（与 test_scene_layer_animation 同款）
        host.installEventFilter(layer)
        new_size = host.size().grownBy(__import__("PyQt5.QtCore", fromlist=["QMargins"]).QMargins(100, 80, 0, 0))
        host.resize(new_size)
        QApplication.sendEvent(host, QResizeEvent(new_size, host.size()))
        assert layer._image_bake_timer is not None
        assert layer._image_bake_timer.isActive(), "几何变化应调度防抖重烘"
        layer._bake_image_now()
        assert layer._image_bake_key != old_key

    def test_bake_skip_same_geometry(self, harness, tmp_path):
        """同几何重复调度不重复烘焙（key 幂等）"""
        host, layer, source, _clock = harness
        p = self._write_png(tmp_path)
        layer.apply_config({"image": p, "blur": 8}, image_resolver=lambda p_: p)
        label = layer._image_label
        pm_before = label.pixmap()
        layer._schedule_image_bake()
        assert label.pixmap() is pm_before


class TestGLFallback:
    """GL 承载方案已验证无效并撤除（Qt5 合成模型下 QOpenGLWidget 的
    framebuffer blit 回 backing store，上层 raster 控件每帧仍被覆盖重绘）。
    保留本说明防回头路；on_animation_frame 只做直通转发。"""

    def test_frame_pass_through(self, harness):
        host, layer, source, _clock = harness
        layer.apply_config(
            {"animation": {"src": "a.webp", "fps": 12}}, image_resolver=lambda p: p
        )
        assert isinstance(layer._anim_view, _AnimView)
        new_frame = QImage(10, 10, QImage.Format_ARGB32)
        layer.on_animation_frame(new_frame)
        assert layer._anim_view._frame is new_frame

    def test_anim_view_render_smoke(self, harness):
        """raster 动画视图离屏 render 一帧不崩（paintEvent 全路径）"""
        host, layer, source, _clock = harness
        layer.apply_config(
            {"animation": {"src": "a.webp", "fps": 12, "fit": "contain"}},
            image_resolver=lambda p: p,
        )
        view = layer._anim_view
        view.resize(320, 200)
        view.set_opacity(0.6)
        img = _paint_one_frame(view)
        assert not img.isNull()
