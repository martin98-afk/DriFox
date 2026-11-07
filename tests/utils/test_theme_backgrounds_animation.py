# -*- coding: utf-8 -*-
"""主题背景动画字段归一化测试

覆盖 backgrounds.<region>.animation 的字段补全、非法值回落、fps clamp，
以及新 chat 区块与旧 scene 区块的合并规则。
"""

from app.utils import theme_manager as tm

_TID = "__anim_normalize__"

def _put(monkeypatch, backgrounds):
    monkeypatch.setitem(tm.theme_manager._themes, _TID, {"backgrounds": backgrounds})

def _bg(monkeypatch, backgrounds):
    _put(monkeypatch, backgrounds)
    return tm.theme_manager.get_theme_backgrounds(_TID)

class TestAnimationNormalization:
    def test_defaults_filled(self, monkeypatch):
        bgs = _bg(
            monkeypatch,
            {"chat": {"animation": {"src": "bg/a.webp"}}},
        )
        anim = bgs["chat"]["animation"]
        assert anim is not None
        assert anim["src"] == "bg/a.webp"
        assert anim["canvas"] == "independent"
        assert anim["fps"] == 12
        assert anim["blend"] == "normal"
        assert anim["fit"] == "cover"
        assert anim["position"] == "center"
        assert anim["opacity"] == 1.0
        assert anim["enabled"] is True

    def test_fps_clamped_to_cap(self, monkeypatch):
        bgs = _bg(monkeypatch, {"chat": {"animation": {"src": "a.webp", "fps": 60}}})
        assert bgs["chat"]["animation"]["fps"] == 15

    def test_fps_lower_bound(self, monkeypatch):
        bgs = _bg(monkeypatch, {"chat": {"animation": {"src": "a.webp", "fps": 0}}})
        assert bgs["chat"]["animation"]["fps"] == 1

    def test_invalid_enum_falls_back(self, monkeypatch):
        bgs = _bg(
            monkeypatch,
            {
                "chat": {
                    "animation": {
                        "src": "a.webp",
                        "blend": "screen",
                        "fit": "stretch",
                        "position": "left",
                    }
                }
            },
        )
        anim = bgs["chat"]["animation"]
        assert anim["blend"] == "normal"
        assert anim["fit"] == "cover"
        assert anim["position"] == "center"

    def test_missing_src_yields_none(self, monkeypatch):
        bgs = _bg(monkeypatch, {"chat": {"animation": {"fps": 12}}})
        assert bgs["chat"]["animation"] is None

    def test_shared_canvas_flag(self, monkeypatch):
        bgs = _bg(monkeypatch, {"chat": {"animation": {"src": "a.webp", "canvas": "shared"}}})
        assert bgs["chat"]["animation"]["canvas"] == "shared"

    def test_disabled_kept(self, monkeypatch):
        bgs = _bg(monkeypatch, {"chat": {"animation": {"src": "a.webp", "enabled": False}}})
        assert bgs["chat"]["animation"]["enabled"] is False

class TestChatRegionMapping:
    def test_chat_merges_into_scene(self, monkeypatch):
        """chat 是 scene 的新名字：两者同时存在时逐字段覆盖"""
        bgs = _bg(
            monkeypatch,
            {
                "scene": {"image": "old.png", "dim": "rgba(0,0,0,.3)"},
                "chat": {"animation": {"src": "new.webp"}},
            },
        )
        assert bgs["chat"] is bgs["scene"], "chat 与 scene 应指向同一归一化结果"
        assert bgs["scene"]["image"] == "old.png"
        assert bgs["scene"]["dim"] == "rgba(0,0,0,.3)"
        assert bgs["scene"]["animation"]["src"] == "new.webp"

    def test_chat_overrides_scene_field(self, monkeypatch):
        bgs = _bg(
            monkeypatch,
            {"scene": {"image": "old.png"}, "chat": {"image": "new.png"}},
        )
        assert bgs["scene"]["image"] == "new.png"

    def test_window_and_sidebar_support_animation(self, monkeypatch):
        bgs = _bg(
            monkeypatch,
            {
                "window": {"animation": {"src": "w.webp", "opacity": 0.6}},
                "sidebar": {"animation": {"src": "s.webp", "canvas": "shared"}},
            },
        )
        assert bgs["window"]["animation"]["src"] == "w.webp"
        assert bgs["window"]["animation"]["opacity"] == 0.6
        assert bgs["sidebar"]["animation"]["canvas"] == "shared"
