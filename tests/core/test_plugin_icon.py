# -*- coding: utf-8 -*-
"""测试 PluginInfo.icon_config 属性"""
import os
from pathlib import Path
from unittest.mock import MagicMock

from app.plugins.managers.plugin_manager import PluginInfo


def test_icon_config_cached_per_instance(tmp_path: Path, monkeypatch):
    """同实例二次访问命中缓存：realpath 只触一次文件系统，返回同一对象。

    ★ 性能（2026-11-03）：icon_config 是 tab_panel/设置卡图标渲染热路径，
    不缓存时每次访问付 2~5 次 FS 调用（watchdog 实测打开设置阻塞 519ms，
    栈正停在 icon_config 内的 os.path.realpath）。
    """
    (tmp_path / "icon.svg").write_text("<svg></svg>", encoding="utf-8")
    info = PluginInfo(name="test", manifest={"icon": "icon.svg"}, path=tmp_path, plugin_type="system")

    calls = []
    real_realpath = os.path.realpath

    def _spy(path, **kw):
        calls.append(path)
        return real_realpath(path, **kw)

    # pathlib resolve 走 os.path.realpath（Windows 即 ntpath.realpath），patch 它
    monkeypatch.setattr(os.path, "realpath", MagicMock(side_effect=_spy))

    cfg1 = info.icon_config
    assert cfg1 is not None
    n_first = len(calls)
    assert n_first > 0, "首次访问应真实触达文件系统"
    cfg2 = info.icon_config
    assert cfg2 is cfg1, "二次访问应返回缓存对象"
    assert len(calls) == n_first, "二次访问不应再触文件系统"


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
