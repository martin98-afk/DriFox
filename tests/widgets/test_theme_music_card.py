# -*- coding: utf-8 -*-
"""主题音乐卡：无音乐隐藏、歌单行、当前播放高亮、单曲开关落库"""
import pytest
from PySide6.QtCore import QObject, Signal


class _FakePlayer(QObject):
    trackChanged = Signal(str)
    tracksChanged = Signal()

    def __init__(self):
        super().__init__()
        self.disabled = ()
        self._current = ""

    def current_track(self) -> str:
        return self._current

    def set_disabled_tracks(self, disabled):
        self.disabled = tuple(disabled)


@pytest.fixture()
def fake_player(qapp, monkeypatch):
    from app.widgets.cards.settings.theme_music_card import ThemeMusicCard

    player = _FakePlayer()
    monkeypatch.setattr(ThemeMusicCard, "_player", staticmethod(lambda: player))
    yield player
    player.deleteLater()


def _make_card(qapp):
    from app.utils.config import Settings
    from app.widgets.cards.settings.theme_music_card import ThemeMusicCard

    return ThemeMusicCard(Settings.get_instance().ui_music_volume)


def test_card_hidden_when_theme_has_no_music(fake_player, monkeypatch):
    from app.utils import theme_music_store

    monkeypatch.setattr(theme_music_store, "theme_tracks", lambda theme_id: ())
    card = _make_card(None)
    try:
        assert card.isHidden() is True
        assert card._rows == []
    finally:
        card.deleteLater()


def test_card_lists_tracks_and_marks_playing(fake_player, monkeypatch):
    from app.utils import theme_music_store
    from app.utils.theme_manager import theme_manager

    tracks = (("/x/a.mp3", "a.mp3"), ("/x/b.mp3", "b.mp3"))
    monkeypatch.setattr(theme_music_store, "theme_tracks", lambda theme_id: tracks)
    monkeypatch.setattr(theme_music_store, "load_disabled", lambda theme_id: frozenset())
    monkeypatch.setattr(theme_manager, "list_themes", lambda: {"forest": "森林日落"})
    fake_player._current = "/x/a.mp3"
    card = _make_card(None)
    try:
        assert card.isHidden() is False
        assert [path for path, _row in card._rows] == ["/x/a.mp3", "/x/b.mp3"]
        assert card._rows[0][1]._playing is True
        assert card._rows[1][1]._playing is False
        assert "正在播放 a.mp3" in card.card.contentLabel.text()
    finally:
        card.deleteLater()


def test_toggling_off_track_persists_and_pushes_to_player(fake_player, monkeypatch):
    from app.utils import theme_music_store

    tracks = (("/x/a.mp3", "a.mp3"), ("/x/b.mp3", "b.mp3"))
    monkeypatch.setattr(theme_music_store, "theme_tracks", lambda theme_id: tracks)
    monkeypatch.setattr(theme_music_store, "load_disabled", lambda theme_id: frozenset())
    saved = []
    monkeypatch.setattr(
        theme_music_store, "save_disabled", lambda theme_id, paths: saved.append((theme_id, set(paths)))
    )
    card = _make_card(None)
    try:
        card._rows[1][1].switch.setChecked(False)
        assert saved and saved[-1][1] == {"/x/b.mp3"}
        assert fake_player.disabled == ("/x/b.mp3",)
    finally:
        card.deleteLater()
