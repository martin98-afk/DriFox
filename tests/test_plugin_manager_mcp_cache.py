"""回归测试：插件 rescan 必须失效 MCP 服务器列表缓存。

根因：PluginManager.get_mcp_servers() 有 30s TTL 缓存，而热重载时
rescan_plugin()/rescan() 只更新 _plugins 却没失效该缓存，导致
MCPListSettingCard._refresh() 与 disconnect_missing() 都读到旧列表——
表现为「列表不刷新」「删除插件后其 MCP 子进程仍残留」。
"""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_pm():
    """构造一个最小化、不依赖真实文件系统的 PluginManager 实例。"""
    from app.plugins.managers.plugin_manager import PluginManager

    pm = PluginManager.__new__(PluginManager)
    pm._initialized = True
    pm._plugins = {}
    pm._mcp_servers_cache = ["stale-entry"]
    pm._mcp_servers_cache_time = time.monotonic()
    pm._app_data_dir = None
    pm._SYSTEM_PLUGIN_DIR = Path("/nonexistent-system")
    pm._CLAUDE_USER_SKILLS_DIR = Path("/nonexistent-claude")
    pm._CLAUDE_PLUGIN_CACHE_DIR = Path("/nonexistent-claude-cache")
    pm._USER_PLUGIN_DIR_NAME = "plugins"
    return pm


class TestRescanInvalidatesMcpCache:
    def test_rescan_plugin_new_plugin_invalidates(self):
        """新插件（_plugins 中不存在）→ 发现后失效缓存"""
        pm = _make_pm()
        with patch.object(pm, "_discover_and_register", return_value=None):
            pm.rescan_plugin("brand-new-plugin")
        assert pm._mcp_servers_cache is None

    def test_rescan_plugin_dir_gone_invalidates(self):
        """插件目录已删除 → 移除后失效缓存"""
        pm = _make_pm()
        fake_info = MagicMock()
        fake_info.path = Path("/nonexistent-dir-x")
        pm._plugins["gone"] = fake_info
        pm.rescan_plugin("gone")
        assert "gone" not in pm._plugins
        assert pm._mcp_servers_cache is None

    def test_rescan_plugin_normal_rescan_invalidates(self):
        """正常重扫（manifest 存在）→ 仍失效缓存"""
        pm = _make_pm()
        fake_info = MagicMock()
        # 用真实存在的目录，避免触发「目录不存在→移除」分支
        fake_info.path = Path(__file__).parent
        fake_info.plugin_type = "user"
        pm._plugins["keep"] = fake_info
        new_info = MagicMock()
        with patch.object(pm, "_scan_one_plugin_dir", return_value=new_info):
            pm.rescan_plugin("keep")
        assert pm._plugins["keep"] is new_info
        assert pm._mcp_servers_cache is None

    def test_rescan_full_invalidates(self):
        """全量 rescan → 失效缓存"""
        pm = _make_pm()
        with patch.object(pm, "_scan_plugins", return_value=[]), patch.object(
            pm, "_restore_enabled_from_settings", return_value=None
        ):
            pm.rescan()
        assert pm._mcp_servers_cache is None
