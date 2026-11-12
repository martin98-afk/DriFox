# -*- coding: utf-8 -*-
"""子智能体默认模型走插件数据目录（config.json）的闭环守卫。

设置卡与 /subagents 命令写 ``subagent_model``，管理器每次派任务时读同一个键并
交给 model.config_for 解析；空值回退当前会话模型。
"""

import importlib.util
import os
from pathlib import Path

_ROOT = os.path.abspath(".")
_MANAGER_PATH = Path(_ROOT) / "plugins" / "system-subagent" / "manager.py"


def _load_manager_module():
    spec = importlib.util.spec_from_file_location("system_subagent_manager_test", _MANAGER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_create_manager_reads_subagent_model_from_plugin_config():
    module = _load_manager_module()
    calls = []

    class _Pool(dict):
        pass

    pool = _Pool()

    def _read(key, default=""):
        calls.append((key, default))
        return "OpenAI 主号:gpt-4o" if key == "subagent_model" else default

    def _config_for(query):
        calls.append(("resolve", query))
        return {"模型名称": "gpt-4o"}

    pool["plugin.config.read"] = _read
    pool["model.config_for"] = _config_for

    manager = module.create_manager(backend=object(), caps=pool)

    assert manager._get_llm_config() == {"模型名称": "gpt-4o"}
    assert ("subagent_model", "") in calls
    assert ("resolve", "OpenAI 主号:gpt-4o") in calls


def test_create_manager_falls_back_to_current_model_when_config_empty():
    module = _load_manager_module()

    class _Pool(dict):
        pass

    pool = _Pool()
    pool["plugin.config.read"] = lambda key, default="": ""
    pool["model.config"] = lambda: {"模型名称": "current"}

    manager = module.create_manager(backend=object(), caps=pool)

    assert manager._get_llm_config() == {"模型名称": "current"}


def test_manifest_declares_model_field_in_plugin_config_schema():
    import json

    manifest = json.loads(
        (Path(_ROOT) / "plugins" / "system-subagent" / ".drifox-plugin" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )
    field = manifest["config_schema"]["fields"][0]

    assert field["key"] == "subagent_model"
    assert field["type"] == "model"
    assert field["default"] == ""
    assert "plugin.config.read" in manifest["capabilities"]
    assert "plugin.config.write" in manifest["capabilities"]
    assert "config.read" in manifest["capabilities"]
    assert "config.write" in manifest["capabilities"]


def _load_command_handler():
    import importlib

    ui_pkg = "system_subagent_ui_pkg"
    ui_path = Path(_ROOT) / "plugins" / "system-subagent" / "ui"
    init_path = ui_path / "__init__.py"
    if ui_pkg not in __import__("sys").modules:
        spec = importlib.util.spec_from_file_location(ui_pkg, init_path, submodule_search_locations=[str(ui_path)])
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        __import__("sys").modules[ui_pkg] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{ui_pkg}.command_handler")


def _migration_env(monkeypatch, *, plugin_value="", legacy_value=""):
    """迁移用的假能力池：读写都记在内存里，不碰真实磁盘。"""
    handler = _load_command_handler()
    state = {"plugin": plugin_value, "legacy": legacy_value, "reads": [], "writes": []}

    class _Pool(dict):
        pass

    pool = _Pool()

    def _plugin_read(key, default=""):
        state["reads"].append(("plugin", key))
        return state["plugin"] if key == "subagent_model" else default

    def _plugin_write(key, value, save=True):
        state["writes"].append(("plugin", key, value))
        if key == "subagent_model":
            state["plugin"] = value
        return True

    def _config_read(key, default=""):
        state["reads"].append(("config", key))
        return state["legacy"] if key == "llm_subagent_default_model" else default

    def _config_write(key, value, save=True):
        state["writes"].append(("config", key, value))
        if key == "llm_subagent_default_model":
            state["legacy"] = value
        return True

    pool["plugin.config.read"] = _plugin_read
    pool["plugin.config.write"] = _plugin_write
    pool["config.read"] = _config_read
    pool["config.write"] = _config_write

    monkeypatch.setattr(
        "app.plugins.capabilities.resolve.pool_for", lambda *a, **k: pool
    )
    return handler, state


def test_migrate_legacy_default_model(monkeypatch):
    handler, state = _migration_env(monkeypatch, legacy_value="OpenAI 主号:gpt-4o")

    handler._migrate_legacy_default_model()

    assert state["plugin"] == "OpenAI 主号:gpt-4o"
    assert state["legacy"] == ""
    assert ("plugin", "subagent_model", "OpenAI 主号:gpt-4o") in state["writes"]
    assert ("config", "llm_subagent_default_model", "") in state["writes"]


def test_migrate_skips_when_plugin_config_already_set(monkeypatch):
    handler, state = _migration_env(
        monkeypatch, plugin_value="MiniMax:MiniMax-M2.7", legacy_value="OpenAI 主号:gpt-4o"
    )

    handler._migrate_legacy_default_model()

    assert state["writes"] == []
    assert state["legacy"] == "OpenAI 主号:gpt-4o"


def test_migrate_skips_when_legacy_value_empty(monkeypatch):
    handler, state = _migration_env(monkeypatch)

    handler._migrate_legacy_default_model()

    assert state["writes"] == []


def test_read_default_model_goes_through_capability_pool(monkeypatch):
    handler, state = _migration_env(monkeypatch, plugin_value="MiniMax:MiniMax-M2.7")

    assert handler._read_default_model() == "MiniMax:MiniMax-M2.7"
    assert ("plugin", "subagent_model") in state["reads"]
