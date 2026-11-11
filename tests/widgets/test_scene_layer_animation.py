# -*- coding: utf-8 -*-
"""SceneLayer 动画背景层测试

不依赖真实动画素材：用假 source 验证挂载、订阅、清理与降级路径。
真实动画 WebP 的解码由 anim_background 单测覆盖。
"""

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QImage, QResizeEvent
from PySide6.QtWidgets import QApplication, QWidget

class _FakeSource:
    def __init__(self, failed=False):
        self.failed = failed
        self.fps = 12
        self.current = None if failed else QImage(4, 4, QImage.Format_ARGB32)
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
    monkeypatch.setattr(sl.AnimationRegistry, "instance", classmethod(lambda cls: _FakeRegistry(source)))
    monkeypatch.setattr(sl.BackgroundClock, "instance", classmethod(lambda cls: clock))

    host = QWidget()
    host.resize(800, 600)
    layer = sl.SceneLayer(host)
    layer.setGeometry(host.rect())
    yield host, layer, source, clock
    layer.clear()
    host.deleteLater()

def _anim(src="bg/a.webp", **kw):
    cfg = {"src": src, "fps": 12}
    cfg.update(kw)
    return cfg

class TestAnimationMount:
    def test_animation_creates_view_and_subscribes(self, harness):
        _host, layer, source, clock = harness
        layer.apply_config({"animation": _anim()}, image_resolver=lambda p: p)
        assert layer._anim_view is not None
        assert source.subs == [layer]
        assert clock.tracked == [source]

    def test_frame_pushed_to_view(self, harness):
        _host, layer, source, _clock = harness
        layer.apply_config({"animation": _anim()}, image_resolver=lambda p: p)
        img = QImage(8, 8, QImage.Format_ARGB32)
        layer.on_animation_frame(img)
        assert layer._anim_view._frame is img

    def test_reapply_without_animation_unsubscribes(self, harness):
        _host, layer, source, _clock = harness
        layer.apply_config({"animation": _anim()}, image_resolver=lambda p: p)
        layer.apply_config({"color": "#123456"}, image_resolver=lambda p: p)
        assert layer._anim_view is None
        assert source.subs == []

    def test_clear_unsubscribes(self, harness):
        _host, layer, source, _clock = harness
        layer.apply_config({"animation": _anim()}, image_resolver=lambda p: p)
        layer.clear()
        assert source.subs == []

    def test_disabled_animation_ignored(self, harness):
        _host, layer, source, _clock = harness
        layer.apply_config(
            {"animation": _anim(enabled=False)}, image_resolver=lambda p: p
        )
        assert layer._anim_view is None
        assert source.subs == []

    def test_failed_source_degrades(self, qapp, monkeypatch):
        """素材解码失败时不建层，保留静态图/底色（降级链）"""
        from app.widgets import scene_layer as sl

        bad = _FakeSource(failed=True)
        monkeypatch.setattr(sl.AnimationRegistry, "instance", classmethod(lambda cls: _FakeRegistry(bad)))
        monkeypatch.setattr(sl.BackgroundClock, "instance", classmethod(lambda cls: _FakeClock()))

        host = QWidget()
        layer = sl.SceneLayer(host)
        layer.apply_config({"animation": _anim()}, image_resolver=lambda p: p)
        assert layer._anim_view is None
        host.deleteLater()

class TestResizeSync:
    """窗口/侧栏 resize 同步链（动画层是绝对定位子控件，不参与布局）

    回归背景：_apply_region_animation 此前漏装 installEventFilter，
    webp 动画背景停在创建时的尺寸，窗口拉大后露底。
    """

    def test_parent_resize_via_event_filter(self, harness):
        """宿主装 filter 后 resize，层与动画视图几何跟随"""
        host, layer, source, _clock = harness
        layer.apply_config({"animation": _anim()}, image_resolver=lambda p: p)
        host.installEventFilter(layer)
        host.resize(1000, 750)
        # 未 show 的顶层 widget resize() 只改尺寸不派发 Resize 事件（原生
        # 句柄未创建），真实窗口已 show 必然派发；此处补同步派发走真实
        # eventFilter 链
        QApplication.sendEvent(host, QResizeEvent(QSize(1000, 750), QSize(800, 600)))
        assert (layer.width(), layer.height()) == (1000, 750)
        assert (layer._anim_view.width(), layer._anim_view.height()) == (1000, 750)

    def test_sync_geometry_manual_after_missed_resize(self, harness):
        """漏掉的 resize 由宿主手动 sync_geometry 收拢（resize 节流阶段二路径）"""
        host, layer, source, _clock = harness
        layer.apply_config({"animation": _anim()}, image_resolver=lambda p: p)
        host.resize(1024, 768)  # 未装 filter，层不跟随（复现原始缺陷）
        assert (layer.width(), layer.height()) == (800, 600)
        layer.sync_geometry()
        assert (layer.width(), layer.height()) == (1024, 768)
        assert (layer._anim_view.width(), layer._anim_view.height()) == (1024, 768)

class TestAnimViewFit:
    def test_cover_center_crop(self, qapp):
        """cover：源 640x360 画到 800x600，只取中间 480x360"""
        from app.widgets.scene_layer import _AnimView

        view = _AnimView()
        view.configure("cover", "center")
        rect = view.source_rect((640, 360), (800, 600))
        assert (rect.x(), rect.y(), rect.width(), rect.height()) == (80, 0, 480, 360)

    def test_cover_position_aligns_vertically(self, qapp):
        """竖图在横窗里：top/bottom 决定取哪一段"""
        from app.widgets.scene_layer import _AnimView

        view = _AnimView()
        expected = {"top": 0, "center": 250, "bottom": 500}
        for position, sy in expected.items():
            view.configure("cover", position)
            rect = view.source_rect((1000, 1000), (400, 200))
            assert rect.y() == sy, f"position={position} 起点错"
            assert rect.height() == 500

    def test_zero_size_is_safe(self, qapp):
        from app.widgets.scene_layer import _AnimView

        view = _AnimView()
        rect = view.source_rect((0, 0), (800, 600))
        assert rect.width() >= 1 and rect.height() >= 1

    def test_contain_and_tile_do_not_crash(self, qapp):
        from PySide6.QtGui import QImage as _QImage, QPainter

        from app.widgets.scene_layer import _AnimView

        for fit in ("contain", "tile"):
            view = _AnimView()
            view.resize(320, 200)
            view.configure(fit, "top")
            view.set_frame(QImage(64, 36, QImage.Format_ARGB32))
            canvas = _QImage(320, 200, _QImage.Format_ARGB32_Premultiplied)
            painter = QPainter(canvas)
            view.render(painter)
            painter.end()
