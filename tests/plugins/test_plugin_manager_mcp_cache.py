"""回归测试：插件 rescan 必须失效 MCP 服务器列表缓存。

根因：PluginManager.get_mcp_servers() 有 30s TTL 缓存，而热重载时
rescan_plugin()/rescan() 只更新 _plugins 却没失效该缓存，导致
MCPListSettingCard._refresh() 与 disconnect_missing() 都读到旧列表——
表现为「列表不刷新」「删除插件后其 MCP 子进程仍残留」。
"""

import json
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


class TestMcpConfigBomTolerance:
    """回归：.mcp.json 带 UTF-8 BOM 时 get_mcp_servers 仍能解析出 server。

    根因：read_text(encoding="utf-8") 对 BOM 开头文件抛
    "Unexpected UTF-8 BOM"（实测日志 2026-11-01 bili-scroll），
    异常被 except 吞掉后该文件的全部 MCP server 被静默跳过。
    修复：encoding 改 "utf-8-sig"（有 BOM 剥 BOM，无 BOM 行为不变）。
    """

    @staticmethod
    def _make_pm_with_mcp():
        from app.plugins.managers.plugin_manager import PluginManager

        pm = PluginManager.__new__(PluginManager)
        pm._initialized = True
        pm._plugins = {}
        pm._mcp_servers_cache = None
        pm._mcp_servers_cache_time = 0.0
        return pm

    def test_bom_config_parses_and_servers_nonempty(self, tmp_path):
        """带 UTF-8 BOM 的 .mcp.json → 解析成功且 server 列表非空"""
        from app.plugins.managers.plugin_manager import PluginManager

        mcp_file = tmp_path / "with-bom" / ".mcp.json"
        mcp_file.parent.mkdir()
        payload = {"mcpServers": {"bili": {"command": "uvx", "args": ["mcp-server"]}}}
        mcp_file.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8"))

        pm = self._make_pm_with_mcp()
        with patch.object(PluginManager, "get_mcp_configs", return_value=[mcp_file]):
            servers = pm.get_mcp_servers()

        assert len(servers) == 1
        assert servers[0]["name"] == "bili"
        assert servers[0]["command"] == "uvx"

    def test_no_bom_config_unaffected(self, tmp_path):
        """无 BOM 的 .mcp.json → 行为不变（回归）"""
        from app.plugins.managers.plugin_manager import PluginManager

        mcp_file = tmp_path / "no-bom" / ".mcp.json"
        mcp_file.parent.mkdir()
        payload = {"mcpServers": {"bili": {"command": "uvx", "args": ["mcp-server"]}}}
        mcp_file.write_text(json.dumps(payload), encoding="utf-8")

        pm = self._make_pm_with_mcp()
        with patch.object(PluginManager, "get_mcp_configs", return_value=[mcp_file]):
            servers = pm.get_mcp_servers()

        assert len(servers) == 1
        assert servers[0]["name"] == "bili"
        assert servers[0]["command"] == "uvx"
