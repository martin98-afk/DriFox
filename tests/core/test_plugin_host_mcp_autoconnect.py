# -*- coding: utf-8 -*-
"""回归测试：启动延迟链必须初始化 MCP 自动发现 + 连接

根因（2026-08-28 bug）：aa8f7a6b 重构把插件管理从 ChatBackend 迁到
PluginHostService 时，_discover_mcp_servers / _init_mcp_connections 的
定义迁入了 PluginHostService，但原 ChatBackend 延迟初始化尾部的调用点
没有随迁 → 全工程零调用。表现为：启动时已启用的 MCP 服务器不会自动
连接，必须手动关闭+开启（走 UI connect_single 路径）才启动。

修复：_do_deferred（延迟 2s 非关键初始化链）尾部补 MCP 自动发现 + 连接，
与 gateway sync_platforms() 补启、LSP start_all_background 同构。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt5.QtCore import QObject

from app.core.plugin_host_service import PluginHostService


def _make_host() -> PluginHostService:
    svc = PluginHostService.__new__(PluginHostService)
    QObject.__init__(svc)
    svc._agent_manager = None
    return svc


@pytest.fixture()
def deferred_spied(monkeypatch):
    """屏蔽 _do_deferred 中除 MCP 外的全部副作用；捕获 2s 延迟回调供手动触发"""
    fired = []

    def _fake_single_shot(msec, callback):
        fired.append((msec, callback))

    monkeypatch.setattr("PyQt5.QtCore.QTimer.singleShot", _fake_single_shot)

    svc = _make_host()
    monkeypatch.setattr(svc, "_reload_themes_from_plugins", lambda: None)
    monkeypatch.setattr(svc, "_start_plugin_watcher", lambda: None)

    # _do_deferred 内部函数级 import 的模块依赖 → patch 源模块
    import app.plugins.builtin_reloaders as br
    import app.plugins.kernel as kernel
    import app.plugins.loaders.plugin_tool_loader as ptl
    import app.plugins.loaders.provider_loader as pl
    import app.plugins.loaders.runtime_component_loader as rcl
    import app.plugins.registries.provider_registry as pr
    import app.core.gateway_service as gs
    import app.core.lsp.lsp_manager as lm

    monkeypatch.setattr(br, "bind_runtime", lambda *a, **k: None)
    monkeypatch.setattr(br, "register_builtin_reloaders", lambda *a, **k: None)
    monkeypatch.setattr(kernel, "get_reloader_registry", lambda: MagicMock())
    monkeypatch.setattr(ptl, "ensure_plugin_tool_watcher", lambda: None)
    monkeypatch.setattr(pl, "ensure_provider_watcher", lambda: None)
    monkeypatch.setattr(pr.ProviderRegistry, "ensure_loaded", lambda self: None)
    monkeypatch.setattr(rcl, "warmup_runtime_components", lambda: None)
    monkeypatch.setattr(gs.GatewayService, "sync_platforms", lambda self: None)

    lsp_mgr = MagicMock()
    monkeypatch.setattr(lm, "get_lsp_manager", lambda: lsp_mgr)

    return svc, fired


class TestDeferredChainInitializesMcp:
    def test_do_deferred_calls_discover_and_connect(self, monkeypatch, deferred_spied):
        """延迟初始化链必须触发 MCP 自动发现 + 连接（调用点随 aa8f7a6b 迁移丢失）"""
        svc, fired = deferred_spied
        calls = []
        monkeypatch.setattr(svc, "_discover_mcp_servers", lambda: calls.append("discover"))
        monkeypatch.setattr(svc, "_init_mcp_connections", lambda: calls.append("connect"))

        svc._defer_non_critical_plugin_init(MagicMock())

        assert fired, "非关键初始化延迟回调未注册"
        _, callback = fired[0]
        callback()  # 手动触发 _do_deferred

        assert calls == ["discover", "connect"], (
            "启动延迟链缺失 MCP 自动发现/连接调用 —— aa8f7a6b 迁移丢调用点回归"
        )

    def test_discover_and_connect_methods_exist(self):
        """两个 MCP 初始化方法必须是 PluginHostService 成员（迁移完整性）"""
        assert hasattr(PluginHostService, "_discover_mcp_servers")
        assert hasattr(PluginHostService, "_init_mcp_connections")


class TestInitMcpConnectionsSemantics:
    """_init_mcp_connections 真实守卫语义（调用链 spy 之外的内在正确性）"""

    def test_connects_enabled_servers(self, monkeypatch):
        import app.tools.mcp_tools as mt
        import app.plugins.managers.plugin_manager as pmm
        import app.utils.config as cfgmod

        svc = _make_host()

        mcp_mgr = MagicMock()
        mcp_mgr.is_connected = False
        monkeypatch.setattr(mt.MCPClientManager, "get_instance", lambda: mcp_mgr)

        pm = MagicMock()
        pm.get_mcp_servers.return_value = [{"name": "srv", "enabled": True}]
        monkeypatch.setattr(pmm.PluginManager, "get_instance", lambda: pm)

        cfg = MagicMock()
        cfg.mcp_enabled = SimpleNamespace(value=True)
        monkeypatch.setattr(cfgmod.Settings, "get_instance", lambda: cfg)

        svc._init_mcp_connections()
        mcp_mgr.connect_all_background.assert_called_once()

    def test_skip_when_global_off(self, monkeypatch):
        import app.tools.mcp_tools as mt
        import app.utils.config as cfgmod

        svc = _make_host()

        mcp_mgr = MagicMock()
        mcp_mgr.is_connected = False
        monkeypatch.setattr(mt.MCPClientManager, "get_instance", lambda: mcp_mgr)

        cfg = MagicMock()
        cfg.mcp_enabled = SimpleNamespace(value=False)
        monkeypatch.setattr(cfgmod.Settings, "get_instance", lambda: cfg)

        svc._init_mcp_connections()
        mcp_mgr.connect_all_background.assert_not_called()

    def test_skip_when_already_connected(self, monkeypatch):
        import app.tools.mcp_tools as mt

        svc = _make_host()

        mcp_mgr = MagicMock()
        mcp_mgr.is_connected = True
        monkeypatch.setattr(mt.MCPClientManager, "get_instance", lambda: mcp_mgr)

        svc._init_mcp_connections()
        mcp_mgr.connect_all_background.assert_not_called()
