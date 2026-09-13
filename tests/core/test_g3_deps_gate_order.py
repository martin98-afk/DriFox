# -*- coding: utf-8 -*-
"""G3：deps 注入门禁顺序对齐（_scan_one_plugin_dir 热路径）。

版本契约不满足的插件不得注入 deps 到 sys.path；兼容插件正常注入。
sys.path 用快照 diff 断言；插件目录全部 tmp_path，不触碰真实插件。
"""
import sys
from pathlib import Path

import pytest

from app.plugins.managers.plugin_manager import PluginManager


def _make_plugin(root: Path, name: str, min_host_version: str = None) -> Path:
    import json

    d = root / name / ".drifox-plugin"
    d.mkdir(parents=True)
    manifest = {"name": name, "version": "1.0"}
    if min_host_version:
        manifest["min_host_version"] = min_host_version
    (d / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    # deps 目录 + 平台目录（ensure_deps_on_path 注入对象）
    deps = root / name / "deps"
    (deps / "windows").mkdir(parents=True)
    (deps / "marker.py").write_text("MARKER = 1\n", encoding="utf-8")
    return root / name


def _scan(pm: PluginManager, plugin_dir: Path):
    return pm._scan_one_plugin_dir(plugin_dir, "user")


def test_blocked_plugin_deps_not_injected(tmp_path):
    """min_host_version 不满足 → 插件被门禁拦截，deps 不进 sys.path。"""
    pm = PluginManager.__new__(PluginManager)
    plugin_dir = _make_plugin(tmp_path, "g3-blocked", min_host_version="999.0.0")
    snapshot = list(sys.path)
    try:
        info = _scan(pm, plugin_dir)
        # 插件仍被发现但被标记拦截
        assert info is not None and info.version_compatible is False and info.load_blocked
        # deps 未注入：sys.path 无本插件 deps 相关新增
        new_entries = [p for p in sys.path if p not in snapshot]
        assert not any("g3-blocked" in p for p in new_entries)
    finally:
        sys.path[:] = snapshot


def test_compatible_plugin_deps_injected(tmp_path):
    """兼容插件 → deps 正常注入（门禁放行路径不回归）。"""
    pm = PluginManager.__new__(PluginManager)
    plugin_dir = _make_plugin(tmp_path, "g3-allowed")
    snapshot = list(sys.path)
    try:
        info = _scan(pm, plugin_dir)
        assert info is not None and not info.load_blocked
        new_entries = [p for p in sys.path if p not in snapshot]
        assert any("g3-allowed" in p and "deps" in p for p in new_entries)
    finally:
        sys.path[:] = snapshot
