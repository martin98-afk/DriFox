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


def test_disabled_tracks_are_not_queued(player, tmp_path):
    """用户关掉的曲目不进 playlist，但列表身份仍是全量"""
    a = _mk(tmp_path, ["a.mp3", "b.mp3"])
    player.apply_config({"tracks": a, "fade_ms": 0}, resolver=lambda p: p, disabled=[a[1]])
    assert player.current_tracks() == tuple(a)
    assert player._playlist.mediaCount() == 1
    assert player.current_track() == a[0]


def test_disable_playing_track_advances_to_next(player, tmp_path):
    a = _mk(tmp_path, ["a.mp3", "b.mp3"])
    player.apply_config({"tracks": a, "fade_ms": 0}, resolver=lambda p: p)
    assert player.current_track() == a[0]
    player._calls.clear()
    player.set_disabled_tracks([a[0]])
    assert player.current_track() == a[1]
    assert "play" in player._calls


def test_disable_all_tracks_keeps_identity_and_resumes(player, tmp_path):
    """全部关掉 → 停止但保留列表；重新开 → 从第一条启用曲恢复"""
    a = _mk(tmp_path, ["a.mp3", "b.mp3"])
    player.apply_config({"tracks": a, "fade_ms": 0}, resolver=lambda p: p)
    player.set_disabled_tracks(a)
    assert player.current_track() == ""
    assert player.current_tracks() == tuple(a)
    assert player._playlist.mediaCount() == 0
    player._calls.clear()
    player.set_disabled_tracks([])
    assert player.current_track() == a[0]
    assert "play" in player._calls


def test_track_changed_signal_reports_current(player, tmp_path):
    seen = []
    player.trackChanged.connect(seen.append)
    a = _mk(tmp_path, ["a.mp3"])
    player.apply_config({"tracks": a, "fade_ms": 0}, resolver=lambda p: p)
    assert seen[-1] == a[0]
    player.stop()
    assert seen[-1] == ""

def test_all_disabled_invalid_media_keeps_identity_and_no_recursive_stop(
    player, tmp_path, monkeypatch
):
    """全禁用态后端同步上报 InvalidMedia：不得递归 stop()、不得清掉列表身份

    本机 DirectShow 0x80040266 现场：_rebuild_enabled_playlist 的 clear() 期间
    后端同步发 InvalidMedia，旧逻辑在「_tracks 非空 + playlist 空」这一全禁用
    独有状态下调 stop()，stop() 又 clear() → 无限递归（启动即闪退）。
    """
    from PyQt5.QtMultimedia import QMediaPlayer

    a = _mk(tmp_path, ["a.mp3", "b.mp3"])
    orig_clear = player._playlist.clear
    calls = {"clear": 0}

    def clear_then_backend_error():
        orig_clear()
        calls["clear"] += 1
        # 后端只在移除 current media 时上报；限次防真栈溢出把测试进程带崩
        if calls["clear"] <= 3:
            player._player.mediaStatusChanged.emit(QMediaPlayer.InvalidMedia)

    monkeypatch.setattr(player._playlist, "clear", clear_then_backend_error)
    player.apply_config({"tracks": a, "fade_ms": 0}, resolver=lambda p: p, disabled=a)

    assert player.current_track() == ""
    # 一次 InvalidMedia 不能把全禁用态的列表身份洗掉，否则「重新勾上即恢复」失效
    assert player.current_tracks() == tuple(a)
    # clear 只应由 _rebuild_enabled_playlist 调用一次，回调不得再触发（递归）
    assert calls["clear"] == 1
