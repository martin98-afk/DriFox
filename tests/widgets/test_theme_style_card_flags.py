# -*- coding: utf-8 -*-
"""主题卡左上角媒体角标识别测试

动态背景有两种承载：旧 overlay.video 与新 animation（动画 WebP）。
角标必须两种都认，否则新主题在卡片上看不出是动态的。
"""

from app.utils import theme_manager as tm
from app.widgets.cards.settings.theme_style_card import collect_theme_entries, _has_animation

_TID = "__card_flag__"

def _entry(backgrounds, mode="dark"):
    return {
        "name": "角标测试",
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

class TestMediaFlags:
    def test_animation_marks_video(self, monkeypatch):
        e = _collect(monkeypatch, {"chat": {"animation": {"src": "a.webp", "enabled": True}}})
        assert e is not None and e["has_video"] is True

    def test_animation_in_window_or_sidebar_also_counts(self, monkeypatch):
        for region in ("window", "sidebar"):
            e = _collect(monkeypatch, {region: {"animation": {"src": "a.webp"}}})
            assert e["has_video"] is True, f"{region} 区域的动画未识别"

    def test_disabled_animation_ignored(self, monkeypatch):
        e = _collect(monkeypatch, {"chat": {"animation": {"src": "a.webp", "enabled": False}}})
        assert e["has_video"] is False

    def test_legacy_overlay_video_still_recognized(self, monkeypatch):
        e = _collect(monkeypatch, {"overlay": {"video": "x.mp4", "enabled": True}})
        assert e["has_video"] is True

    def test_static_theme_has_no_flag(self, monkeypatch):
        e = _collect(monkeypatch, {"chat": {"image": "bg.png"}})
        assert e["has_video"] is False

    def test_music_flag_independent(self, monkeypatch):
        e = _collect(
            monkeypatch,
            {"chat": {"animation": {"src": "a.webp"}}, "music": {"tracks": ["a.mp3"]}},
        )
        assert e["has_video"] is True
        assert e["has_music"] is True

class TestHasAnimationHelper:
    def test_chat_and_scene_share_object(self):
        bgs = {"chat": {"animation": {"src": "a.webp"}}, "scene": {"animation": {"src": "a.webp"}}}
        assert _has_animation(bgs) is True

    def test_empty_backgrounds(self):
        assert _has_animation({}) is False
