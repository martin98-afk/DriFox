# -*- coding: utf-8 -*-
"""#31：插件目录签名扫描延迟化行为测试。

契约：发现缓存命中路径不同步算签名（置 None + 2s 后台补算被调度）；补算
幂等（rescan 已写回则跳过）；补算前 rescan 现场算兜底打穿全扫并写回；
补算完成后 rescan 正常短路。
"""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_pm():
    """最小化 PluginManager 实例（沿用 mcp_cache 测试的构造口径）。"""
    from app.plugins.managers.plugin_manager import PluginManager

    pm = PluginManager.__new__(PluginManager)
    pm._initialized = True
    pm._plugins = {}
    pm._app_data_dir = None
    pm._last_scan_signature = None
    pm._SYSTEM_PLUGIN_DIR = Path("/nonexistent-system")
    pm._CLAUDE_USER_SKILLS_DIR = Path("/nonexistent-claude")
    pm._CLAUDE_PLUGIN_CACHE_DIR = Path("/nonexistent-claude-cache")
    pm._USER_PLUGIN_DIR_NAME = "plugins"
    return pm


def test_backfill_computes_when_none():
    pm = _make_pm()
    with patch.object(pm, "_plugins_dir_signature", return_value=("sig-a",)) as m:
        pm._backfill_scan_signature()
    assert pm._last_scan_signature == ("sig-a",)
    assert m.call_args.kwargs.get("include_mtime") is False


def test_backfill_idempotent_when_already_set():
    pm = _make_pm()
    pm._last_scan_signature = ("rescan-wrote",)
    with patch.object(pm, "_plugins_dir_signature") as m:
        pm._backfill_scan_signature()
    m.assert_not_called()  # rescan 已先行写回，补算跳过
    assert pm._last_scan_signature == ("rescan-wrote",)


def test_initialize_hit_defers_signature(monkeypatch):
    """命中路径：签名置 None + singleShot(2000, 补算) 被调度，不同步算。"""
    from app.plugins.managers.plugin_manager import PluginManager

    pm = PluginManager.__new__(PluginManager)
    pm._initialized = False
    pm._plugins = {}
    pm._app_data_dir = None
    pm._last_scan_signature = None
    pm._pending_config_cards = {}
    pm._disabled_components_cache = None
    pm._SYSTEM_PLUGIN_DIR = Path("/nonexistent-system")
    pm._CLAUDE_USER_SKILLS_DIR = Path("/nonexistent-claude")
    pm._CLAUDE_PLUGIN_CACHE_DIR = Path("/nonexistent-claude-cache")
    pm._USER_PLUGIN_DIR_NAME = "plugins"

    scheduled = []

    class _FakeQTimer:
        @staticmethod
        def singleShot(msec, callback):
            scheduled.append((msec, callback))

    monkeypatch.setattr("PyQt5.QtCore.QTimer", _FakeQTimer)
    monkeypatch.setattr(pm, "_migrate_split_system_states", lambda: None)
    monkeypatch.setattr(pm, "_try_load_from_discovery_cache", lambda: True)
    monkeypatch.setattr(pm, "_restore_enabled_from_settings", lambda: None)
    monkeypatch.setattr(pm, "_plugins_dir_signature", lambda include_mtime=True: ("sig-hit",))

    pm.initialize()
    assert pm._initialized is True
    assert pm._last_scan_signature is None  # 不同步算
    assert len(scheduled) == 1
    delay, cb = scheduled[0]
    assert delay == 2000
    cb()  # 手动触发补算
    assert pm._last_scan_signature == ("sig-hit",)


def test_rescan_falls_through_when_none_then_writes_back():
    """补算前 rescan：现场算签名 ≠ None → 打穿全扫 → 全扫后写回。"""
    pm = _make_pm()
    pm._plugins = {"keep": MagicMock()}
    with patch.object(pm, "_plugins_dir_signature", return_value=("sig-now",)) as m_sig, patch.object(
        pm, "_scan_plugins", return_value=[]
    ) as m_scan, patch.object(pm, "_merge_discovered", return_value=({}, [])), patch.object(
        pm, "_apply_deps_conflict_gate", return_value=None
    ):
        result = pm.rescan()
    assert m_sig.call_count == 1
    assert m_scan.call_count > 0  # 打穿全扫
    assert pm._last_scan_signature == ("sig-now",)  # 写回
    assert result["added"] == [] and result["changed"] == []  # keep 被移除属正常 diff

    # 第二次 rescan：现场算同签名 == 驻留 → 短路，零全扫
    # （首轮全扫把 keep 判 removed 清空了 _plugins，短路判据要求非空，重塞）
    pm._plugins = {"keep": MagicMock()}
    with patch.object(pm, "_plugins_dir_signature", return_value=("sig-now",)) as m_sig2, patch.object(
        pm, "_scan_plugins"
    ) as m_scan2, patch.object(pm, "_merge_discovered", return_value=({}, [])), patch.object(
        pm, "_apply_deps_conflict_gate", return_value=None
    ):
        pm.rescan()
    assert m_sig2.call_count == 1
    assert m_scan2.call_count == 0  # 短路命中


def test_rescan_shortcircuits_after_backfill():
    """补算完成后 rescan：现场算同签名 → 正常短路（收益路径）。"""
    pm = _make_pm()
    pm._plugins = {"keep": MagicMock()}  # 短路判据要求 _plugins 非空
    with patch.object(pm, "_plugins_dir_signature", return_value=("sig-stable",)):
        pm._backfill_scan_signature()
    with patch.object(pm, "_plugins_dir_signature", return_value=("sig-stable",)) as m_sig, patch.object(
        pm, "_scan_plugins"
    ) as m_scan:
        result = pm.rescan()
    assert result == {"added": [], "removed": [], "changed": []}
    assert m_scan.call_count == 0
