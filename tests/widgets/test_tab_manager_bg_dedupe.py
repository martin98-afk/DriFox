# -*- coding: utf-8 -*-
"""主题背景去重键回归：键必须用解析后的绝对路径

回归背景：``_apply_region_animation`` / ``_apply_area_bg`` 的缓存键用 YAML
里的原始相对路径，而 25+ 个插件动态主题都写 ``../../assets/chat.webp``、
chrome-classics 两个主题都写 ``sidebar.jpg``。跨主题切换时字符串相同 →
命中缓存直接 return，旧图层留在原地：配色换了、动态背景/背景图不换。
"""

import pytest
from PyQt5.QtWidgets import QWidget


@pytest.fixture(autouse=True)
def reset_singleton():
    from app.tray_manager import TrayManager
    from app.widgets.tab_manager_window import TabManagerWindow

    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    yield
    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None


@pytest.fixture()
def tm(qtbot):
    from app.widgets.tab_manager_window import TabManagerWindow

    win = TabManagerWindow.create_instance()
    qtbot.addWidget(win)
    return win


class _FakeSceneLayer(QWidget):
    """只记录构造，不碰真实动画解码"""

    created = []

    def __init__(self, parent=None, radius=0):
        super().__init__(parent)
        _FakeSceneLayer.created.append(self)

    def apply_config(self, cfg, image_resolver=None):
        self.cfg = cfg

    def set_canvas_provider(self, provider):
        pass

    def clear(self):
        pass

    def sync_geometry(self):
        pass


ANIM = {
    "src": "../../assets/chat.webp",
    "fps": 15,
    "canvas": "shared",
    "opacity": 1.0,
}


def _patch_resolver(monkeypatch, tm, mapping):
    monkeypatch.setattr(
        type(tm),
        "_resolve_theme_image",
        staticmethod(lambda image: mapping.get(image, image)),
    )


class TestAnimationKey:
    def test_same_relative_path_different_file_rebuilds(self, tm, monkeypatch):
        """两个插件主题写同一个相对路径 → 解析到不同文件时必须重建图层"""
        from app.widgets import scene_layer as sl

        _FakeSceneLayer.created = []
        monkeypatch.setattr(sl, "SceneLayer", _FakeSceneLayer)
        paths = {"../../assets/chat.webp": "/plugins/a/assets/chat.webp"}
        _patch_resolver(monkeypatch, tm, paths)

        tm._apply_region_animation("window", tm, {"animation": dict(ANIM)})
        first = tm._window_anim_layer
        assert first is _FakeSceneLayer.created[-1]

        # 切到另一个插件主题：YAML 字符串完全相同，素材文件不同
        paths["../../assets/chat.webp"] = "/plugins/b/assets/chat.webp"
        tm._apply_region_animation("window", tm, {"animation": dict(ANIM)})

        assert tm._window_anim_layer is not first
        assert len(_FakeSceneLayer.created) == 2

    def test_same_resolved_file_keeps_layer(self, tm, monkeypatch):
        """解析到同一素材（同插件深浅主题共用）仍去重，不重建"""
        from app.widgets import scene_layer as sl

        _FakeSceneLayer.created = []
        monkeypatch.setattr(sl, "SceneLayer", _FakeSceneLayer)
        _patch_resolver(monkeypatch, tm, {"../../assets/chat.webp": "/plugins/a/assets/chat.webp"})

        tm._apply_region_animation("window", tm, {"animation": dict(ANIM)})
        first = tm._window_anim_layer
        tm._apply_region_animation("window", tm, {"animation": dict(ANIM)})

        assert tm._window_anim_layer is first
        assert len(_FakeSceneLayer.created) == 1


class TestStaticBgKey:
    def test_same_relative_path_different_file_rebuilds(self, tm, monkeypatch):
        """chrome-classics 两个主题都写 sidebar.jpg → 必须各自重建"""
        paths = {"sidebar.jpg": "/themes/forest-sunset/sidebar.jpg"}
        _patch_resolver(monkeypatch, tm, paths)
        cfg = {"image": "sidebar.jpg", "opacity": 0.72, "color": "rgba(8, 20, 32, 230)"}

        tm._apply_area_bg("sidebar", tm._tab_frame, dict(cfg))
        first = tm._sidebar_bg_label
        assert first is not None

        paths["sidebar.jpg"] = "/themes/shaded-purple/sidebar.jpg"
        tm._apply_area_bg("sidebar", tm._tab_frame, dict(cfg))

        assert tm._sidebar_bg_label is not first

    def test_same_resolved_file_keeps_label(self, tm, monkeypatch):
        _patch_resolver(monkeypatch, tm, {"sidebar.jpg": "/themes/a/sidebar.jpg"})
        cfg = {"image": "sidebar.jpg", "opacity": 0.72, "color": "rgba(8, 20, 32, 230)"}

        tm._apply_area_bg("sidebar", tm._tab_frame, dict(cfg))
        first = tm._sidebar_bg_label
        tm._apply_area_bg("sidebar", tm._tab_frame, dict(cfg))

        assert tm._sidebar_bg_label is first
