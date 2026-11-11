# -*- coding: utf-8 -*-
"""主题音乐单曲开关存储：按插件分组、主题内曲目解析"""
import json

import pytest


@pytest.fixture()
def app_data(tmp_path, monkeypatch):
    """把 app_data 指到临时目录，存储落 tmp/plugin_data/<slug>/"""
    import app.utils.utils as utils_mod

    monkeypatch.setattr(utils_mod, "get_app_data_dir", lambda: tmp_path)
    return tmp_path


def _mk_theme(tmp_path, plugin: str = "my-plugin"):
    """造一个插件主题目录：<plugin>/.drifox-plugin + <plugin>/themes/t/t.yaml"""
    plugin_dir = tmp_path / plugin
    (plugin_dir / ".drifox-plugin").mkdir(parents=True)
    theme_dir = plugin_dir / "themes" / "t"
    theme_dir.mkdir(parents=True)
    yaml_path = theme_dir / "t.yaml"
    yaml_path.write_text("id: t\n", encoding="utf-8")
    return plugin_dir, theme_dir, yaml_path


def test_plugin_slug_from_plugin_theme(tmp_path, monkeypatch):
    from app.utils import theme_music_store as store
    from app.utils.theme_manager import theme_manager

    _plugin_dir, theme_dir, yaml_path = _mk_theme(tmp_path)
    monkeypatch.setattr(
        theme_manager, "get_theme", lambda tid: {"_dir": str(theme_dir), "_path": str(yaml_path)}
    )
    assert store.plugin_slug_for_theme("t") == "my-plugin"


def test_user_theme_falls_back_to_shared_slug(tmp_path, monkeypatch):
    """<app_data>/themes/ 下的自建主题没有插件清单 → user-themes"""
    from app.utils import theme_music_store as store
    from app.utils.theme_manager import theme_manager

    theme_dir = tmp_path / "themes" / "mine"
    theme_dir.mkdir(parents=True)
    monkeypatch.setattr(theme_manager, "get_theme", lambda tid: {"_dir": str(theme_dir), "_path": ""})
    assert store.plugin_slug_for_theme("mine") == "user-themes"


def test_save_and_load_disabled_roundtrip(app_data, monkeypatch):
    from app.utils import theme_music_store as store
    from app.utils.theme_manager import theme_manager

    _plugin_dir, theme_dir, yaml_path = _mk_theme(app_data)
    monkeypatch.setattr(
        theme_manager, "get_theme", lambda tid: {"_dir": str(theme_dir), "_path": str(yaml_path)}
    )

    assert store.load_disabled("t") == frozenset()
    assert store.save_disabled("t", ["/a.mp3", "/b.mp3"]) is True
    assert store.load_disabled("t") == {"/a.mp3", "/b.mp3"}

    stored = json.loads(
        (app_data / "plugin_data" / "my-plugin" / "theme_music.json").read_text(encoding="utf-8")
    )
    assert stored == {"t": ["/a.mp3", "/b.mp3"]}

    # 清空 → 删除该主题键，文件保留空 dict
    assert store.save_disabled("t", []) is True
    assert store.load_disabled("t") == frozenset()


def test_theme_tracks_resolves_and_filters(app_data, monkeypatch, tmp_path):
    from app.utils import theme_music_store as store
    from app.utils.theme_manager import theme_manager

    _plugin_dir, theme_dir, _yaml = _mk_theme(app_data)
    (theme_dir / "a.mp3").write_bytes(b"x")
    (theme_dir / "b.mp3").write_bytes(b"x")
    monkeypatch.setattr(theme_manager, "get_theme_dir", lambda tid: theme_dir)
    monkeypatch.setattr(
        theme_manager,
        "get_theme_backgrounds",
        lambda tid: {
            "music": {
                "tracks": ["a.mp3", "missing.mp3", ":/qrc/x.mp3", "b.mp3", "a.mp3"],
                "enabled": True,
            }
        },
    )

    tracks = store.theme_tracks("t")
    assert [name for _path, name in tracks] == ["a.mp3", "b.mp3"]
    assert all(p.endswith(".mp3") and not p.startswith(":") for p, _ in tracks)


def test_theme_tracks_empty_when_disabled_or_absent(app_data, monkeypatch, tmp_path):
    from app.utils import theme_music_store as store
    from app.utils.theme_manager import theme_manager

    _plugin_dir, theme_dir, _yaml = _mk_theme(app_data)
    (theme_dir / "a.mp3").write_bytes(b"x")
    monkeypatch.setattr(theme_manager, "get_theme_dir", lambda tid: theme_dir)

    monkeypatch.setattr(theme_manager, "get_theme_backgrounds", lambda tid: {"music": None})
    assert store.theme_tracks("t") == ()

    monkeypatch.setattr(
        theme_manager,
        "get_theme_backgrounds",
        lambda tid: {"music": {"tracks": ["a.mp3"], "enabled": False}},
    )
    assert store.theme_tracks("t") == ()
