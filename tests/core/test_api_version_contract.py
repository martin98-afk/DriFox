# -*- coding: utf-8 -*-
"""契约2：api_version 插件 API 契约。

四用例：未声明过 / =1 过 / =99 拦且工具不入 registry / =0 warning+过。
"""
from pathlib import Path

import pytest
from loguru import logger

from app.plugins.managers.plugin_manager import PluginInfo, PluginManager
from app.plugins.loaders.plugin_tool_loader import load_plugin_tools
from app.plugins.version_gate import check_api_version
from app.tools.registry import ToolRegistry


def test_absent_api_version_passes():
    """未声明 api_version → 兼容（老插件零改动）。"""
    ok, reason = check_api_version({})
    assert ok is True and reason == ""
    ok2, _ = check_api_version({"name": "x"})
    assert ok2 is True


def test_api_version_equal_passes():
    """api_version=1（等于宿主契约）→ 过。"""
    ok, reason = check_api_version({"api_version": 1})
    assert ok is True and reason == ""


def test_api_version_99_blocked_and_tools_skipped(tmp_path, monkeypatch, log_capture=None):
    """api_version=99 → 拒载；门禁贯通 loader：工具不入 registry。"""
    ok, reason = check_api_version({"api_version": 99}, "av99")
    assert ok is False
    assert "api_version=99" in reason and "高于宿主" in reason

    # 集成：PluginManager 注册被拦插件 → load_plugin_tools 跳过其工具
    tool_dir = tmp_path / "av99" / "tools"
    tool_dir.mkdir(parents=True)
    (tool_dir / "tool.py").write_text(
        "def register(registry):\n"
        "    registry.register('av99_tool',\n"
        "        {'description': 't', 'parameters': {'type': 'object', 'properties': {}}},\n"
        "        impl=lambda: 'ok', danger='safe')\n",
        encoding="utf-8",
    )
    info = PluginInfo(
        name="av99",
        manifest={"name": "av99", "api_version": 99},
        path=tmp_path / "av99",
        api_compatible=False,
        api_reason=reason,
    )
    assert info.load_blocked is True  # api_compatible 纳入 load_blocked

    pm = PluginManager.__new__(PluginManager)
    pm._plugins = {"av99": info}
    pm._initialized = True
    monkeypatch.setattr(PluginManager, "get_instance", classmethod(lambda cls: pm))

    reg = ToolRegistry.get_instance()
    try:
        loaded = load_plugin_tools(plugin_roots=[tmp_path], root_tracker={})
        assert "av99" not in loaded
        assert reg.get("av99_tool") is None
    finally:
        reg.unregister("av99_tool")


def test_api_version_zero_warns_and_passes(log_capture=None):
    """api_version=0（<1）→ warning 向下兼容放行。"""
    records = []
    sink = logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        ok, reason = check_api_version({"api_version": 0}, "av0")
        assert ok is True and reason == ""
        assert any("api_version=0" in r and "向下兼容" in r for r in records)
    finally:
        logger.remove(sink)
