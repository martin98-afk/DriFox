# -*- coding: utf-8 -*-
"""回归测试：插件热重载安装后 MCP 服务器必须自动补连

根因：MCP 热重载路径（ChatBackend._on_hot_reload_requested → main_widget
_on_plugin_hot_reload）只刷新服务器列表 + 断开孤儿连接（disconnect_missing），
从不主动连接新增的已启用服务器；唯一连接入口 _init_mcp_connections 只在
应用启动时调用一次。表现为插件热重载安装（带 .mcp.json）后配置显示开启
但 MCP 实际未启动。

修复：热重载刷新列表后调用 MCPListSettingCard.refresh_connections()
补连所有「已启用但未连接」的服务器。本测试锁定该方法的补连语义：
- 全局开关关闭 → 不连接
- 已启用且未连接 → 触发 connect_server_background
- 已连接 → 跳过（幂等）
- 服务器自身禁用 → 跳过
"""

from types import SimpleNamespace

from app.widgets.cards.settings.mcp_setting_card import MCPListSettingCard


class _FakeCfg:
    def __init__(self, enabled: bool):
        self.mcp_enabled = SimpleNamespace(value=enabled)


class _FakeMgr:
    """记录 connect_server_background 调用的 MCP 管理器替身"""

    def __init__(self, status_list):
        self._status_list = status_list
        self._connect_calls = []

    def get_status(self):
        return list(self._status_list)

    def connect_server_background(self, name, config, on_done=None):
        self._connect_calls.append((name, config))


def _make_card(enabled=True, servers=None, status_list=None):
    """构造不初始化 Qt 的 MCPListSettingCard，仅替换 refresh_connections 依赖"""
    card = MCPListSettingCard.__new__(MCPListSettingCard)
    card.cfg = _FakeCfg(enabled)
    card._get_servers = lambda: list(servers or [])
    mgr = _FakeMgr(status_list or [])
    card._get_mcp_manager = lambda: mgr
    return card, mgr


def _server(name, enabled=True):
    return {"name": name, "type": "stdio", "command": "echo", "args": [], "enabled": enabled}


def _status(name, connected):
    return {"name": name, "connected": connected}


class TestRefreshConnections:
    def test_global_off_skips_all(self):
        """全局开关关闭时，即使服务器已启用也不触发连接"""
        card, mgr = _make_card(enabled=False, servers=[_server("srv")])
        card.refresh_connections()
        assert mgr._connect_calls == []

    def test_connects_enabled_unconnected(self):
        """已启用但未连接的服务器 → 触发 connect_server_background"""
        card, mgr = _make_card(enabled=True, servers=[_server("srv")], status_list=[])
        card.refresh_connections()
        assert [name for name, _ in mgr._connect_calls] == ["srv"]

    def test_skips_already_connected(self):
        """已连接的服务器 → 跳过（幂等，避免重复启动）"""
        card, mgr = _make_card(
            enabled=True,
            servers=[_server("srv")],
            status_list=[_status("srv", connected=True)],
        )
        card.refresh_connections()
        assert mgr._connect_calls == []

    def test_skips_disabled_server(self):
        """服务器自身 enabled=False → 跳过，不连接"""
        card, mgr = _make_card(
            enabled=True,
            servers=[_server("off", enabled=False), _server("on")],
            status_list=[],
        )
        card.refresh_connections()
        assert [name for name, _ in mgr._connect_calls] == ["on"]

    def test_mixed_connects_only_missing(self):
        """混合场景：只补连缺失的，已连接的保持不动"""
        card, mgr = _make_card(
            enabled=True,
            servers=[_server("a"), _server("b"), _server("c")],
            status_list=[_status("a", connected=True), _status("b", connected=False)],
        )
        card.refresh_connections()
        assert sorted(name for name, _ in mgr._connect_calls) == ["b", "c"]
