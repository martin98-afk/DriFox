# -*- coding: utf-8 -*-
"""backgrounds.overlay 区块解析：normalize 字段、缺省路径"""
from pathlib import Path

from app.utils.theme_manager import ThemeManager

OVERLAY_YAML = """
name: 测试主题
id: overlay-test
mode: light
backgrounds:
  scene:
    color: '#F8F4ED'
    enabled: true
  overlay:
    video: leaves-overlay.mp4
    opacity: 0.28
    brightness_boost: rgba(255, 253, 247, 0.12)
    enabled: true
colors:
  text_primary: '#333333'
"""


def _load_tmp_theme(tmp_path: Path, text: str) -> ThemeManager:
    theme_dir = tmp_path / "overlay-test"
    theme_dir.mkdir(parents=True)
    yaml_file = theme_dir / "overlay-test.yaml"
    yaml_file.write_text(text, encoding="utf-8")
    tm = ThemeManager()  # 单例，直接复用
    tm._load_yaml(yaml_file, theme_dir, is_builtin=True)
    return tm


def test_overlay_block_normalized(tmp_path):
    tm = _load_tmp_theme(tmp_path, OVERLAY_YAML)
    bgs = tm.get_theme_backgrounds("overlay-test")
    ov = bgs["overlay"]
    assert ov is not None
    assert ov["video"] == "leaves-overlay.mp4"
    assert abs(ov["opacity"] - 0.28) < 1e-6
    assert ov["brightness_boost"] == "rgba(255, 253, 247, 0.12)"
    assert ov["enabled"] is True
    # 其余区块不受影响
    assert bgs["scene"]["color"] == "#F8F4ED"


def test_overlay_absent_returns_none(tmp_path):
    tm = _load_tmp_theme(tmp_path, "name: 无覆盖\nid: no-overlay\ncolors:\n  text_primary: '#333'\n")
    bgs = tm.get_theme_backgrounds("no-overlay")
    assert bgs["overlay"] is None


MUSIC_YAML = """
name: 音乐测试
id: music-test
mode: dark
backgrounds:
  overlay:
    video: leaves.mp4
    target: sidebar
  music:
    tracks:
      - ../shared/rain.mp3
      - bgm-2.mp3
    volume: 0.45
    loop: false
    fade_ms: 120
colors:
  text_primary: '#eeeeee'
"""

def test_overlay_target_passthrough(tmp_path):
    tm = _load_tmp_theme(tmp_path, MUSIC_YAML)
    ov = tm.get_theme_backgrounds("music-test")["overlay"]
    assert ov["target"] == "sidebar"

def test_overlay_target_absent_is_none(tmp_path):
    """缺 target 时归一化结果为 None，由挂载方回落 chat_area"""
    tm = _load_tmp_theme(tmp_path, OVERLAY_YAML)
    ov = tm.get_theme_backgrounds("overlay-test")["overlay"]
    assert ov["target"] is None

def test_music_block_passthrough(tmp_path):
    tm = _load_tmp_theme(tmp_path, MUSIC_YAML)
    music = tm.get_theme_backgrounds("music-test")["music"]
    assert music is not None
    assert music["tracks"] == ["../shared/rain.mp3", "bgm-2.mp3"]
    assert abs(music["volume"] - 0.45) < 1e-6
    assert music["loop"] is False
    assert music["fade_ms"] == 120
    assert music["enabled"] is True  # 未写时补默认

def test_music_tracks_string_becomes_list(tmp_path):
    """tracks 写成单个字符串时归一为单元素列表"""
    tm = _load_tmp_theme(tmp_path, """
name: 单曲
id: single-track
backgrounds:
  music:
    tracks: only.mp3
colors:
  text_primary: '#eee'
""")
    music = tm.get_theme_backgrounds("single-track")["music"]
    assert music["tracks"] == ["only.mp3"]

def test_music_absent_returns_none(tmp_path):
    tm = _load_tmp_theme(tmp_path, OVERLAY_YAML)
    assert tm.get_theme_backgrounds("overlay-test")["music"] is None
