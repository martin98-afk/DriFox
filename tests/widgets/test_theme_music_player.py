# -*- coding: utf-8 -*-
"""ThemeMusicPlayer：列表身份去重、循环模式、音量优先级、焦点暂停、失效曲跳过

播放器不真的解码音频（测试环境无音频设备），断言围绕「播放器被怎么调用了」
——patch 掉 QMediaPlayer 的方法，记录到 _calls。
"""
import pytest


@pytest.fixture()
def player(qapp, monkeypatch):
    from app.widgets import theme_music_player as mod

    # 隔离单例，避免测试间串状态
    monkeypatch.setattr(mod.ThemeMusicPlayer, "_instance", None, raising=False)
    p = mod.ThemeMusicPlayer()
    p._player.play = lambda: p._calls.append("play")
    p._player.pause = lambda: p._calls.append("pause")
    p._player.stop = lambda: p._calls.append("stop")
    p._player.setVolume = lambda v: p._calls.append(("setVolume", int(v)))
    p._player.setMedia = lambda *a: p._calls.append("setMedia")
    yield p
    p.deleteLater()


def _mk(tmp_path, names):
    paths = []
    for n in names:
        f = tmp_path / n
        f.write_bytes(b"x")
        paths.append(str(f))
    return paths


def test_same_track_list_does_not_restart(player, tmp_path):
    a = _mk(tmp_path, ["a.mp3", "b.mp3"])
    cfg = {"tracks": a, "fade_ms": 0}
    player.apply_config(cfg, resolver=lambda p: p)
    player._calls.clear()
    player.apply_config(cfg, resolver=lambda p: p)
    assert "play" not in player._calls
    assert "stop" not in player._calls


def test_different_track_list_restarts(player, tmp_path):
    a = _mk(tmp_path, ["a.mp3"])
    b = _mk(tmp_path, ["b.mp3"])
    player.apply_config({"tracks": a, "fade_ms": 0}, resolver=lambda p: p)
    player._calls.clear()
    player.apply_config({"tracks": b, "fade_ms": 0}, resolver=lambda p: p)
    assert "play" in player._calls


def test_none_config_stops(player, tmp_path):
    a = _mk(tmp_path, ["a.mp3"])
    player.apply_config({"tracks": a, "fade_ms": 0}, resolver=lambda p: p)
    player._calls.clear()
    player.apply_config(None, resolver=lambda p: p)
    assert "stop" in player._calls
    assert player.current_tracks() == ()


def test_disabled_config_stops(player, tmp_path):
    a = _mk(tmp_path, ["a.mp3"])
    player.apply_config({"tracks": a, "fade_ms": 0}, resolver=lambda p: p)
    player._calls.clear()
    player.apply_config({"tracks": a, "enabled": False}, resolver=lambda p: p)
    assert "stop" in player._calls


def test_missing_tracks_all_filtered_stops(player, tmp_path):
    player.apply_config({"tracks": ["/no/such/file.mp3"], "fade_ms": 0},
                        resolver=lambda p: p)
    assert player.current_tracks() == ()


def test_qrc_tracks_are_skipped(player, tmp_path):
    """QtMultimedia 读不到 qrc，音乐路径明确不支持；全被过滤后不播"""
    player.apply_config({"tracks": [":/audio/a.mp3"], "fade_ms": 0},
                        resolver=lambda p: p)
    assert player.current_tracks() == ()


def test_loop_maps_to_playlist_mode(player, tmp_path):
    a = _mk(tmp_path, ["a.mp3", "b.mp3"])
    player.apply_config({"tracks": a, "loop": True, "fade_ms": 0},
                        resolver=lambda p: p)
    assert player.playlist_mode_name() == "Loop"
    player.apply_config({"tracks": a, "loop": False, "fade_ms": 0},
                        resolver=lambda p: p)
    assert player.playlist_mode_name() == "CurrentItemOnce"


def test_user_volume_overrides_theme_volume(player, tmp_path):
    a = _mk(tmp_path, ["a.mp3"])
    player.set_volume(0.2)  # 用户先调过
    player._calls.clear()
    player.apply_config({"tracks": a, "volume": 0.9, "fade_ms": 0},
                        resolver=lambda p: p)
    assert ("setVolume", 20) in player._calls


def test_theme_volume_used_when_user_never_set(player, tmp_path):
    a = _mk(tmp_path, ["a.mp3"])
    player._calls.clear()
    player.apply_config({"tracks": a, "volume": 0.35, "fade_ms": 0},
                        resolver=lambda p: p)
    assert ("setVolume", 35) in player._calls


def test_app_inactive_pauses_and_resume_plays(player, tmp_path):
    a = _mk(tmp_path, ["a.mp3"])
    player.apply_config({"tracks": a, "fade_ms": 0}, resolver=lambda p: p)
    player._calls.clear()
    player.set_app_active(False)
    assert "pause" in player._calls
    player._calls.clear()
    player.set_app_active(True)
    assert "play" in player._calls


def test_resume_does_not_play_when_user_stopped(player, tmp_path):
    a = _mk(tmp_path, ["a.mp3"])
    player.apply_config({"tracks": a, "fade_ms": 0}, resolver=lambda p: p)
    player.stop()
    player._calls.clear()
    player.set_app_active(False)
    player.set_app_active(True)
    assert "play" not in player._calls


def test_invalid_media_advances_to_next(player, tmp_path):
    a = _mk(tmp_path, ["a.mp3", "b.mp3"])
    player.apply_config({"tracks": a, "fade_ms": 0}, resolver=lambda p: p)
    player._calls.clear()
    player._on_media_status(8)  # QMediaPlayer.InvalidMedia
    assert "play" in player._calls


def test_fade_used_when_fade_ms_positive(player, tmp_path):
    """fade_ms>0 时音量从 0 起、结束时回到有效音量"""
    a = _mk(tmp_path, ["a.mp3"])
    player._calls.clear()
    player.apply_config({"tracks": a, "volume": 0.8, "fade_ms": 200},
                        resolver=lambda p: p)
    volumes = [c[1] for c in player._calls if isinstance(c, tuple) and c[0] == "setVolume"]
    assert volumes, "淡入过程应有多次 setVolume"
    assert volumes[-1] == 80
    assert min(volumes) >= 0
    assert max(volumes) <= 100
