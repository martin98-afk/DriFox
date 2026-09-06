# -*- coding: utf-8 -*-
"""P2-2：热重载 last-known-good（T8.1 行 17 口径）。

四用例：坏版本重载失败旧版仍可用 / 修好自动恢复新版 / 静默写入 30s 内签名轮询兜底
恢复 / 新版成功前旧 registry 项不清空。
mtime 轮询用注入时钟/直接改 mtime，不真等 30s。
"""
import os
from pathlib import Path

import pytest
from loguru import logger

from app.plugins.loaders.plugin_tool_loader import PluginToolWatcher
from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
from app.tools.registry import ToolRegistry

GOOD_V1 = (
    "def register(registry):\n"
    "    registry.register('lg_tool',\n"
    "        {'description': 't', 'parameters': {'type': 'object', 'properties': {}}},\n"
    "        impl=lambda: 'v1', danger='safe')\n"
)
GOOD_V2 = GOOD_V1.replace("'v1'", "'v2'")
BAD = "raise RuntimeError('broken reload')\n" + GOOD_V1


@pytest.fixture()
def log_capture():
    records = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    yield records
    logger.remove(sink_id)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时插件 + watcher + Settings 启用（finally 还原）。"""
    from app.utils.config import Settings

    tools = tmp_path / "lg-plug" / "tools"
    tools.mkdir(parents=True)
    (tools / "t.py").write_text(GOOD_V1, encoding="utf-8")
    cfg = Settings.get_instance()
    saved = list(cfg.enabled_plugins.value or [])
    cfg.enabled_plugins.value = saved + ["lg-plug"]
    reg = ToolRegistry.get_instance()
    watcher = PluginToolWatcher(registry=reg, roots=[tmp_path])
    yield tmp_path, watcher, reg
    cfg.enabled_plugins.value = saved
    reg.unregister("lg_tool")


def test_bad_reload_keeps_old_usable(env):
    """坏版本重载失败：旧版仍可用（impl 仍为 v1）。"""
    tmp_path, watcher, reg = env
    watcher.reload_plugin("lg-plug")
    assert reg.get("lg_tool").impl() == "v1"
    # 磁盘换成坏版本（import 即炸）
    (tmp_path / "lg-plug" / "tools" / "t.py").write_text(BAD, encoding="utf-8")
    watcher.reload_plugin("lg-plug")  # 不应抛
    assert reg.get("lg_tool") is not None
    assert reg.get("lg_tool").impl() == "v1"  # last-known-good


def test_registry_item_not_cleared_before_new_success(env):
    """新版成功前：旧 registry 项不清空（对象保留，非占位）。"""
    tmp_path, watcher, reg = env
    watcher.reload_plugin("lg-plug")
    old_entry = reg.get("lg_tool")
    (tmp_path / "lg-plug" / "tools" / "t.py").write_text(BAD, encoding="utf-8")
    watcher.reload_plugin("lg-plug")
    assert reg.get("lg_tool") is old_entry


def test_fixed_version_auto_recovers(env):
    """修好后重载自动恢复新版（impl v2）。"""
    tmp_path, watcher, reg = env
    watcher.reload_plugin("lg-plug")
    (tmp_path / "lg-plug" / "tools" / "t.py").write_text(BAD, encoding="utf-8")
    watcher.reload_plugin("lg-plug")
    assert reg.get("lg_tool").impl() == "v1"
    (tmp_path / "lg-plug" / "tools" / "t.py").write_text(GOOD_V2, encoding="utf-8")
    watcher.reload_plugin("lg-plug")
    assert reg.get("lg_tool").impl() == "v2"


def test_silent_write_signature_poll(tmp_path, monkeypatch, log_capture):
    """静默写入（错过 watchfiles）→ 30s 周期签名比对兜底触发重载。"""
    plug = tmp_path / "sig-plug"
    ui = plug / "ui"
    ui.mkdir(parents=True)
    (ui / "__init__.py").write_text("MARK = 1\n", encoding="utf-8")

    reg_ui = UIPluginRegistry.__new__(UIPluginRegistry)
    reg_ui._ui_signatures = {"sig-plug": reg_ui._compute_ui_signature(plug)}
    reg_ui._plugin_paths = {"sig-plug": str(plug)}
    reloaded_calls = []
    monkeypatch.setattr(reg_ui, "reload_plugin", lambda name, path: reloaded_calls.append(name) or True)

    # 静默写入：新文件 + 直接改 mtime（不真等）
    (ui / "extra.py").write_text("MARK = 2\n", encoding="utf-8")
    future = reg_ui._compute_ui_signature(plug) + 100
    os.utime(ui / "extra.py", (future, future))

    assert reg_ui.poll_silent_ui_changes() == ["sig-plug"]
    assert reloaded_calls == ["sig-plug"]
    # 无变化再轮询：不重复触发
    assert reg_ui.poll_silent_ui_changes() == []
