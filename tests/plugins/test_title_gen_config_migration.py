# -*- coding: utf-8 -*-
"""旧 TitleGenDefaultModel 一次性迁移到插件配置。"""
import types

import orjson as json

from app.utils.config import _migrate_title_gen_default_model


def _instance(tmp_path, legacy):
    path = tmp_path / "app.config"
    raw = {"LLM": {}}
    if legacy is not None:
        raw["LLM"]["TitleGenDefaultModel"] = legacy
    path.write_bytes(json.dumps(raw))
    obj = types.SimpleNamespace(file=path, save=lambda: None)
    return obj


def test_no_legacy_value_skips(tmp_path, monkeypatch):
    from app.plugins.managers import plugin_config_store

    called = []
    monkeypatch.setattr(
        plugin_config_store.PluginConfigStore,
        "set_values",
        lambda self, name, values: called.append((name, values)) or True,
    )
    assert _migrate_title_gen_default_model(_instance(tmp_path, None)) is False
    assert _migrate_title_gen_default_model(_instance(tmp_path, "")) is False
    assert called == []


def test_migrates_legacy_value(tmp_path, monkeypatch):
    from app.plugins.managers import plugin_config_store

    written = []
    monkeypatch.setattr(
        plugin_config_store.PluginConfigStore,
        "get",
        lambda self, name, key: None,
    )
    monkeypatch.setattr(
        plugin_config_store.PluginConfigStore,
        "set_values",
        lambda self, name, values: written.append((name, values)) or True,
    )
    obj = _instance(tmp_path, "OpenCode:gpt-4o")
    saved = []
    obj.save = lambda: saved.append(True)
    assert _migrate_title_gen_default_model(obj) is True
    assert written == [("system-title-generator", {"title_model": "OpenCode:gpt-4o"})]
    assert saved == [True]


def test_existing_plugin_value_wins(tmp_path, monkeypatch):
    from app.plugins.managers import plugin_config_store

    called = []
    monkeypatch.setattr(
        plugin_config_store.PluginConfigStore,
        "get",
        lambda self, name, key: "MiniMax:M2.7",
    )
    monkeypatch.setattr(
        plugin_config_store.PluginConfigStore,
        "set_values",
        lambda self, name, values: called.append((name, values)) or True,
    )
    assert _migrate_title_gen_default_model(_instance(tmp_path, "OpenCode:gpt-4o")) is False
    assert called == []
