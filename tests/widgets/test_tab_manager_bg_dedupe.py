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
    # 两阶段构造契约适配（T6b）：测试访问 _tab_frame 等阶段二产物，需补建
    # （生产路径由 main._show_popup 驱动，幂等）
    win.complete_construction()
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


class TestAreaBgPremult:
    """[I4] _apply_area_bg 摘常驻 QGraphicsOpacityEffect → 预乘入共享缓存

    回归背景：opacity<1 的图片背景原来挂常驻 QGraphicsOpacityEffect，label
    每次重绘（splitter 拖动/窗口缩放）走全幅离屏合成（双倍 blit）；独立
    QPixmap(resolved_image) 解码游离在 scene_pixmap_cache 外，同图多区域
    重复解码。预乘副本 alpha 与 effect 合成数学一致（alpha × opacity）。
    """

    def _apply_png(self, tm, monkeypatch, tmp_path, opacity):
        from PyQt5.QtGui import QImage

        p = str(tmp_path / f"bg_{int(opacity * 255)}.png")
        img = QImage(8, 8, QImage.Format_ARGB32)
        img.fill(0xFF3366AA)  # 不透明底
        img.save(p, "PNG")
        _patch_resolver(monkeypatch, tm, {"bg.png": p})
        tm._apply_area_bg("sidebar", tm._tab_frame, {"image": "bg.png", "opacity": opacity})
        return p

    def test_no_graphics_effect_and_premultiplied_alpha(self, tm, monkeypatch, tmp_path):
        """opacity<1：无 effect、pixmap alpha 预乘（255→184@0.72）"""
        self._apply_png(tm, monkeypatch, tmp_path, 0.72)
        label = tm._sidebar_bg_label
        assert label is not None
        assert label.graphicsEffect() is None, "常驻 effect 必须摘除（预乘替代）"
        pm = label.pixmap()
        assert pm is not None and not pm.isNull()
        image = pm.toImage()
        a = image.pixelColor(0, 0).alpha()
        assert abs(a - int(round(0.72 * 255))) <= 1, f"预乘 alpha 应≈184，实际 {a}"

    def test_shared_cache_entry_across_reapply(self, tm, monkeypatch, tmp_path):
        """同图同 opacity 重复应用 → 命中同一缓存条目（游离解码消除）"""
        from app.core.infra.scene_pixmap_cache import scene_pixmap_cache

        scene_pixmap_cache._users += 1  # 防其他用例计数归零 clear
        try:
            p = self._apply_png(tm, monkeypatch, tmp_path, 0.72)
            first = tm._sidebar_bg_label.pixmap()
            # 幂等键命中直接 return，不走重建；强置键后重走取缓存路径
            tm._last_sidebar_bg_key = None
            tm._apply_area_bg("sidebar", tm._tab_frame, {"image": "bg.png", "opacity": 0.72})
            second = tm._sidebar_bg_label.pixmap()
            assert first is not None and second is not None
            assert first.cacheKey() == second.cacheKey()
            kinds = {sub[0] for bucket in scene_pixmap_cache._groups.values() for sub in bucket}
            assert "area_premult" in kinds
            assert p  # resolver 路径真实存在
        finally:
            scene_pixmap_cache._users = 0
            scene_pixmap_cache.clear()

    def test_opaque_image_shares_scene_source(self, tm, monkeypatch, tmp_path):
        """opacity=1：区块层与 scene 层共用同一 source 条目（跨层共享收益）"""
        from app.core.infra.scene_pixmap_cache import scene_pixmap_cache

        scene_pixmap_cache._users += 1
        try:
            p = self._apply_png(tm, monkeypatch, tmp_path, 1.0)
            pm = tm._sidebar_bg_label.pixmap()
            shared = scene_pixmap_cache.get_pixmap("source", p, load=lambda _: None)
            assert pm is not None and shared is not None
            assert pm.cacheKey() == shared.cacheKey(), "应复用 scene 层的 source 条目，不重解码"
        finally:
            scene_pixmap_cache._users = 0
            scene_pixmap_cache.clear()
