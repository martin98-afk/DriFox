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
