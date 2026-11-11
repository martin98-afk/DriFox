# -*- coding: utf-8 -*-
"""主题卡预览图回退测试：纯动画 WebP 主题用 animation.src 兜底

纯动画主题（只有 backgrounds.<region>.animation.src，无任何静态 image）
此前预览采集不到任何区域，卡片只剩渐变底表现为全黑
（2026-11-08 lucy-edgerunner 实证）。修复后 image 缺失时回退动画 src，
由 QImage 解出首帧做静态预览。
"""

from app.utils import theme_manager as tm
from app.widgets.cards.settings.theme_style_card import (
    _first_background,
    collect_theme_entries,
)

_TID = "__preview_fallback__"


def _entry(backgrounds, mode="dark"):
    return {
        "name": "预览回退测试",
        "id": _TID,
        "mode": mode,
        "window": {"gradient_start": "#111111", "gradient_end": "#222222"},
        "colors": {"accent": "#66c6ff"},
        "backgrounds": backgrounds,
    }


def _collect(monkeypatch, backgrounds, mode="dark"):
    monkeypatch.setitem(tm.theme_manager._themes, _TID, _entry(backgrounds, mode))
    entries = collect_theme_entries(mode == "light")
    return next((e for e in entries if e["id"] == _TID), None)


class TestFirstBackgroundSelection:
    def test_animation_only_region_selected(self):
        """纯动画区域（无静态 image）也能被选为预览源"""
        bgs = {"window": {"animation": {"src": "a.webp", "enabled": True}}, "sidebar": {"color": "#333"}}
        assert _first_background(bgs).get("animation", {}).get("src") == "a.webp"

    def test_static_image_still_preferred_in_scene(self):
        """scene 有静态图时优先 scene，window 动画不抢（保持原语义）"""
        bgs = {
            "scene": {"image": "scene.png", "enabled": True},
            "window": {"animation": {"src": "a.webp", "enabled": True}},
        }
        area = _first_background(bgs)
        assert area.get("image") == "scene.png"
        assert "animation" not in area or not area.get("animation")

    def test_disabled_animation_region_skipped(self):
        """动画禁用的区域不作为预览源"""
        bgs = {"window": {"animation": {"src": "a.webp", "enabled": False}}}
        assert _first_background(bgs) == {}

    def test_disabled_area_skipped(self):
        """整个区域禁用时跳过"""
        bgs = {"window": {"animation": {"src": "a.webp"}, "enabled": False}}
        assert _first_background(bgs) == {}


class TestCollectImageFallback:
    def test_pure_animation_theme_image_falls_back_to_anim_src(self, monkeypatch):
        """纯动画主题：条目 image 回退为 animation.src"""
        e = _collect(monkeypatch, {"window": {"animation": {"src": "anim.webp", "enabled": True}}})
        assert e["image"] == "anim.webp"
        assert e["has_video"] is True

    def test_static_image_not_overridden_by_animation(self, monkeypatch):
        """有静态图的混合主题保持静态图，不被动画 src 覆盖"""
        e = _collect(monkeypatch, {"scene": {"image": "bg.png", "enabled": True}})
        assert e["image"] == "bg.png"

    def test_light_mode_entry_also_falls_back(self, monkeypatch):
        """浅色模式同款回退（lucy-edgerunner-light 同构）"""
        e = _collect(
            monkeypatch,
            {"window": {"animation": {"src": "anim.webp", "enabled": True}}},
            mode="light",
        )
        assert e["image"] == "anim.webp"
