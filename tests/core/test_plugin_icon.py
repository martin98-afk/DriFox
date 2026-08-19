# -*- coding: utf-8 -*-
"""测试 PluginInfo.icon_config 属性"""
from pathlib import Path
from app.plugins.managers.plugin_manager import PluginInfo


def test_icon_config_string(tmp_path: Path):
    """plugin.json with icon: 'icon.svg' string"""
    (tmp_path / "icon.svg").write_text("<svg></svg>")
    manifest = {"name": "test", "icon": "icon.svg"}
    info = PluginInfo(name="test", manifest=manifest, path=tmp_path, plugin_type="system")
    cfg = info.icon_config
    assert cfg is not None
    assert "light" in cfg
    assert "dark" in cfg
    assert cfg["light"].name == "icon.svg"


def test_icon_config_dict(tmp_path: Path):
    """plugin.json with icon: {light/dark} dict"""
    (tmp_path / "light.svg").write_text("<svg></svg>")
    (tmp_path / "dark.svg").write_text("<svg></svg>")
    manifest = {"name": "test", "icon": {"light": "light.svg", "dark": "dark.svg"}}
    info = PluginInfo(name="test", manifest=manifest, path=tmp_path, plugin_type="system")
    cfg = info.icon_config
    assert cfg is not None
    assert cfg["light"].name == "light.svg"
    assert cfg["dark"].name == "dark.svg"


def test_icon_config_no_icon(tmp_path: Path):
    """No icon field → None (no icon.svg either)"""
    info = PluginInfo(name="test", manifest={"name": "test"}, path=tmp_path, plugin_type="system")
    assert info.icon_config is None


def test_icon_config_convention_fallback(tmp_path: Path):
    """No icon field but icon.svg exists → use it"""
    (tmp_path / "icon.svg").write_text("<svg></svg>")
    info = PluginInfo(name="test", manifest={"name": "test"}, path=tmp_path, plugin_type="system")
    cfg = info.icon_config
    assert cfg is not None
    assert cfg["light"].name == "icon.svg"
    assert cfg["dark"].name == "icon.svg"


def test_icon_config_single_theme_fallback(tmp_path: Path):
    """Only light theme specified → dark gets same"""
    (tmp_path / "light.svg").write_text("<svg></svg>")
    manifest = {"name": "test", "icon": {"light": "light.svg"}}
    info = PluginInfo(name="test", manifest=manifest, path=tmp_path, plugin_type="system")
    cfg = info.icon_config
    assert cfg is not None
    assert cfg["light"].name == "light.svg"
    assert cfg["dark"].name == "light.svg"


def test_icon_config_file_not_exists(tmp_path: Path):
    """Icon field points to non-existent file → None"""
    manifest = {"name": "test", "icon": "nonexistent.svg"}
    info = PluginInfo(name="test", manifest=manifest, path=tmp_path, plugin_type="system")
    assert info.icon_config is None
