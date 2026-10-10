# -*- coding: utf-8 -*-
"""[T26/H2] 回归：插件发现持久化缓存四场景。

桩形态对齐 test_plugin_manager_mcp_cache.py（PluginManager.__new__ + tmp 目录），
_scan_plugins 走真实逻辑（tmp 假插件 + 真 manifest 契约链）。

场景：
1. 命中提速：二次 initialize 零全扫 + 副作用重放（deps/config_schema）+ from cache 日志
2. manifest 修改失效：os.utime 改 manifest mtime → 键不等 → full scan 回写新键
3. 缓存损坏兜底：坏 JSON → 全扫成功 + 缓存自修复回写
4. 热扫描作废 + rescan 短路保留：rescan_plugin 删缓存；rescan 签名未变仍短路
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app.plugins.managers.plugin_manager as pm_mod
from app.plugins.managers.plugin_manager import PluginManager


def _make_plugin(base: Path, name: str, manifest_extra: dict | None = None) -> Path:
    d = base / name
    (d / ".drifox-plugin").mkdir(parents=True, exist_ok=True)
    manifest = {"name": name, "version": "1.0.0", "description": f"{name} 插件"}
    manifest.update(manifest_extra or {})
    (d / ".drifox-plugin" / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return d


class _Env:
    """tmp 插件目录环境（system/claude-cache 两个根 + 隔离 data_dir）。"""

    def __init__(self, tmp_path: Path):
        self.system = tmp_path / "plugins-system"
        self.claude = tmp_path / "claude-cache"
        self.data = tmp_path / "data"
        for d in (self.system, self.claude, self.data):
            d.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.data / "cache" / "plugin_discovery.json"


def _make_pm(env: _Env, monkeypatch) -> PluginManager:
    pm = PluginManager.__new__(PluginManager)
    pm._initialized = False
    pm._plugins = {}
    pm._app_data_dir = env.data
    pm._SYSTEM_PLUGIN_DIR = env.system
    pm._CLAUDE_USER_SKILLS_DIR = env.claude
    pm._CLAUDE_PLUGIN_CACHE_DIR = env.claude
    pm._USER_PLUGIN_DIR_NAME = "plugins"
    pm._last_scan_signature = None
    pm._mcp_servers_cache = None
    pm._mcp_servers_cache_time = 0.0

    # Settings 相关：不碰真实单例（allow_user_override 默认 True）
    fake_settings = SimpleNamespace(allow_user_override=SimpleNamespace(value=True))
    monkeypatch.setattr(
        "app.utils.config.Settings.get_instance", classmethod(lambda cls: fake_settings)
    )
    monkeypatch.setattr(pm_mod, "ensure_deps_on_path", lambda p: None)
    # 缓存落盘路径按方案口径 get_app_data_dir()/cache/——patch 指向隔离目录
    monkeypatch.setattr("app.utils.utils.get_app_data_dir", lambda: env.data)
    # initialize/rescan 的 UI 与 Settings 副作用不在本测试面
    monkeypatch.setattr(pm, "_restore_enabled_from_settings", lambda: None)
    monkeypatch.setattr(pm, "_migrate_split_system_states", lambda: None)
    monkeypatch.setattr(pm, "_load_plugin_ui", lambda name: None)
    monkeypatch.setattr(pm, "_unload_plugin_ui", lambda name: None)
    monkeypatch.setattr(pm, "_unregister_config_schema", lambda name: None)
    return pm


def _spy_scan(pm, monkeypatch):
    counter = {"n": 0}
    real = pm_mod.PluginManager._scan_plugins

    def _counted(base_dir, plugin_type):
        counter["n"] += 1
        return real(pm, base_dir, plugin_type)

    monkeypatch.setattr(pm, "_scan_plugins", _counted)
    return counter


def _spy_config_schema(pm, monkeypatch):
    counter = {"n": 0}
    monkeypatch.setattr(pm, "_register_config_schema", lambda name, manifest: counter.__setitem__("n", counter["n"] + 1))
    return counter


def _spy_info_log(monkeypatch):
    msgs = []
    monkeypatch.setattr(pm_mod.logger, "info", lambda m, *a, **k: msgs.append(str(m)))
    return msgs


def test_cache_hit_skips_full_scan_and_replays_side_effects(tmp_path, monkeypatch):
    """场景 1：二次 initialize 零全扫 + 副作用重放 + from cache 日志。"""
    env = _Env(tmp_path)
    _make_plugin(env.system, "sys-a")
    _make_plugin(env.claude, "cl-b")
    _make_plugin(env.data / "plugins", "usr-c")

    pm1 = _make_pm(env, monkeypatch)
    scan1 = _spy_scan(pm1, monkeypatch)
    pm1.initialize(env.data)
    assert scan1["n"] > 0, "首次 initialize 应全扫"
    assert env.cache_file.exists(), "全扫成功后应回写发现缓存"
    assert len(pm1._plugins) == 3

    # 二次 initialize（全新 manager 实例，模拟重启）：命中缓存零全扫
    pm2 = _make_pm(env, monkeypatch)
    scan2 = _spy_scan(pm2, monkeypatch)
    schema2 = _spy_config_schema(pm2, monkeypatch)
    logs = _spy_info_log(monkeypatch)
    pm2.initialize(env.data)

    assert scan2["n"] == 0, "命中缓存不得触发任何 _scan_plugins 全扫"
    assert set(pm2._plugins) == {"sys-a", "cl-b", "usr-c"}, "命中路径应重建全部插件"
    assert schema2["n"] == 3, "副作用重放：每插件应重放 _register_config_schema"
    assert any("from cache" in m for m in logs), "命中必须打 from cache 验收日志"


def test_manifest_modification_invalidates_cache(tmp_path, monkeypatch):
    """场景 2：manifest 文件 mtime/size 变化 → 键不等 → full scan 并回写新键。"""
    env = _Env(tmp_path)
    plugin_dir = _make_plugin(env.system, "sys-a")

    pm1 = _make_pm(env, monkeypatch)
    pm1.initialize(env.data)
    old_key = json.loads(env.cache_file.read_text(encoding="utf-8"))["key"]

    # 修改 manifest 内容 + utime 改 mtime（NTFS 父目录 mtime 陷阱的对向验证）
    mf = plugin_dir / ".drifox-plugin" / "plugin.json"
    mf.write_text(
        json.dumps({"name": "sys-a", "version": "2.0.0"}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.utime(mf, ns=(10**18, 10**18 + 5))

    pm2 = _make_pm(env, monkeypatch)
    scan2 = _spy_scan(pm2, monkeypatch)
    logs = _spy_info_log(monkeypatch)
    pm2.initialize(env.data)

    assert scan2["n"] > 0, "manifest 变化应键不等回退全扫"
    assert any("full scan" in m for m in logs), "键不等应打 full scan 日志"
    new_key = json.loads(env.cache_file.read_text(encoding="utf-8"))["key"]
    assert new_key != old_key, "全扫后应回写新签名键（自修复）"
    assert pm2._plugins["sys-a"].manifest["version"] == "2.0.0", "全扫应取到新 manifest"


def test_corrupted_cache_falls_back_to_full_scan(tmp_path, monkeypatch):
    """场景 3：缓存文件损坏 → 全扫兜底成功 + 缓存自修复回写。"""
    env = _Env(tmp_path)
    _make_plugin(env.system, "sys-a")

    env.cache_file.parent.mkdir(parents=True, exist_ok=True)
    env.cache_file.write_text("{broken json !!!", encoding="utf-8")

    pm = _make_pm(env, monkeypatch)
    scan = _spy_scan(pm, monkeypatch)
    pm.initialize(env.data)

    assert scan["n"] > 0, "缓存损坏应回退全扫"
    assert "sys-a" in pm._plugins, "全扫兜底后插件应正常注册"
    repaired = json.loads(env.cache_file.read_text(encoding="utf-8"))
    assert repaired.get("groups", {}).get("system"), "损坏缓存应被全扫结果自修复回写"


def test_hot_scan_invalidates_and_rescan_shortcut_preserved(tmp_path, monkeypatch):
    """场景 4：rescan_plugin 作废缓存文件；rescan 签名未变仍短路。"""
    env = _Env(tmp_path)
    _make_plugin(env.system, "sys-a")

    pm = _make_pm(env, monkeypatch)
    with patch.object(pm, "_restore_enabled_from_settings", return_value=None):
        pm.initialize(env.data)
    assert env.cache_file.exists()

    # 热扫描（watchfiles 单插件重扫）→ 发现缓存作废
    with patch.object(pm, "_scan_one_plugin_dir", return_value=pm._plugins["sys-a"]):
        pm.rescan_plugin("sys-a")
    assert not env.cache_file.exists(), "rescan_plugin 必须作废发现缓存文件"

    # rescan 签名未变 → 短路（缓存缺失不回写，下次重启全扫一次后重建）
    result = pm.rescan()
    assert result == {"added": [], "removed": [], "changed": []}, "签名未变 rescan 应短路"
    assert not env.cache_file.exists(), "短路路径不回写缓存"
