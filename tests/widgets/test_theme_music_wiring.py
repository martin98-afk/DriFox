# -*- coding: utf-8 -*-
"""音乐接线：_apply_bg_from_theme 驱动播放器、焦点信号转发、音量配置项"""
import pytest


def test_music_config_reaches_player(qapp, monkeypatch):
    """_apply_bg_from_theme 必须把 bgs['music'] 交给 ThemeMusicPlayer"""
    from app.widgets import theme_music_player as tmod

    seen = {}

    class FakePlayer:
        @classmethod
        def instance(cls):
            return cls()

        def apply_config(self, cfg, resolver):
            seen["cfg"] = cfg
            seen["resolver"] = resolver

        def set_app_active(self, active):
            seen["active"] = active

    monkeypatch.setattr(tmod, "ThemeMusicPlayer", FakePlayer)

    from app.widgets.tab_manager_window import TabManagerWindow

    host = type("H", (), {})()
    host._resolve_theme_image = staticmethod(TabManagerWindow._resolve_theme_image)
    host._apply_theme_music = TabManagerWindow._apply_theme_music.__get__(host)

    music = {"tracks": ["a.mp3"], "volume": 0.4}
    host._apply_theme_music(music)
    assert seen["cfg"] is music
    assert callable(seen["resolver"])


def test_no_music_config_stops_player(qapp, monkeypatch):
    from app.widgets import theme_music_player as tmod

    seen = {}

    class FakePlayer:
        @classmethod
        def instance(cls):
            return cls()

        def apply_config(self, cfg, resolver):
            seen["cfg"] = cfg

    monkeypatch.setattr(tmod, "ThemeMusicPlayer", FakePlayer)
    from app.widgets.tab_manager_window import TabManagerWindow

    host = type("H", (), {})()
    host._resolve_theme_image = staticmethod(TabManagerWindow._resolve_theme_image)
    host._apply_theme_music = TabManagerWindow._apply_theme_music.__get__(host)
    host._apply_theme_music(None)
    assert seen["cfg"] is None


def test_focus_change_forwards_to_player(qapp, monkeypatch):
    """applicationStateChanged 转发：ApplicationActive → True，Inactive → False"""
    from PyQt5.QtCore import Qt

    from app.widgets import theme_music_player as tmod

    seen = []

    class FakePlayer:
        @classmethod
        def instance(cls):
            return cls()

        def set_app_active(self, active):
            seen.append(active)

    monkeypatch.setattr(tmod, "ThemeMusicPlayer", FakePlayer)
    from app.widgets.tab_manager_window import TabManagerWindow

    host = type("H", (), {})()
    host._on_application_focus_changed = TabManagerWindow._on_application_focus_changed.__get__(host)
    host._on_application_focus_changed(Qt.ApplicationInactive)
    host._on_application_focus_changed(Qt.ApplicationActive)
    assert seen == [False, True]


def test_music_volume_config_item_exists(qapp):
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    assert hasattr(cfg, "ui_music_volume")
    assert 0 <= cfg.ui_music_volume.value <= 100


def test_music_volume_change_updates_player(qapp, monkeypatch):
    from app.widgets import theme_music_player as tmod

    seen = []

    class FakePlayer:
        @classmethod
        def instance(cls):
            return cls()

        def set_volume(self, v):
            seen.append(v)

    monkeypatch.setattr(tmod, "ThemeMusicPlayer", FakePlayer)

    from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard

    host = type("H", (), {})()
    host._on_music_volume_changed = LLMSettingsCard._on_music_volume_changed.__get__(host)
    host._on_music_volume_changed(35)
    assert seen == [0.35]
