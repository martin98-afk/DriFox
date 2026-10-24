# -*- coding: utf-8 -*-
"""MCP 临时开启（ephemeral）回归测试

背景：mcp_manage 工具要能让模型按需临时开一台已关闭的 MCP，用完显式关闭，
并且**不**把它注入常驻工具列表（省 token）。这条链上有四处容易踩掉临时连接：

1. `_connect_all` 的 stale 清理按 config.enabled 判孤儿 —— 临时开启的
   config.enabled 恒为 false，全量连接/多窗口刷新时会被当孤儿杀掉。
2. `disconnect_missing(valid_names)` 同理，热重载时杀掉临时连接。
3. `get_tool_schemas_by_server()` 必须继续跳过它，否则临时开启就变成常驻注入，
   token 白省。
4. 手动断开 / 全局断开后 `_ephemeral` 必须清干净，否则残留幽灵名字会让
   list 渲染成「临时开启」并让孤儿清理一直跳过它。

本测试锁定上述行为。
"""

import asyncio
import threading

import pytest

from app.tools.mcp_tools import MCPClientManager, MCPServerConnection, MCPState


@pytest.fixture
def mgr():
    """不启动真实事件循环线程的 manager（与 test_mcp_connection_state 同构）"""
    m = MCPClientManager.__new__(MCPClientManager)
    m._connections = {}
    m._connected = False
    m._busy_names = set()
    m._busy_lock = threading.Lock()
    m._connect_all_running = False
    m._connect_all_names = set()
    m._lock = threading.Lock()
    m._loop = None
    m._thread = None
    m._ephemeral = set()

    # fixture 不启动后台事件循环：_run_async 直接在同线程跑协程
    def _run_async(coro, timeout=60):
        return asyncio.run(coro)

    m._run_async = _run_async
    return m


def _conn(name, *, enabled, state=MCPState.CONNECTED, tools=()):
    conn = MCPServerConnection(name, {"name": name, "enabled": enabled, "type": "stdio"})
    conn.set_state(state)
    if state == MCPState.CONNECTED:
        conn.session = object()  # 占位：仅用于 connected / session 判定
    conn.tools = list(tools)
    return conn


class _FakeTool:
    def __init__(self, name, description="", inputSchema=None):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema or {"type": "object", "properties": {}}


@pytest.fixture
def patch_pm(monkeypatch):
    """把 PluginManager.get_instance 换成返回给定 servers 的替身"""

    def _apply(servers):
        import app.plugins.managers.plugin_manager as pm_mod

        class _PM:
            def get_instance(self):
                return self

            def is_initialized(self):
                return True

            def get_mcp_servers(self):
                return list(servers)

        monkeypatch.setattr(pm_mod.PluginManager, "get_instance", lambda: _PM())

    return _apply


# ═══════════════════════════════════════════════════════════
# 1. 临时开启不进常驻注入列表（省 token 的结构性保证）
# ═══════════════════════════════════════════════════════════


class TestEphemeralNotInjected:
    def test_ephemeral_server_excluded_from_common_schemas(self, mgr):
        """临时开启的 config 仍是关闭态 → get_tool_schemas_by_server 必须跳过它"""
        mgr._connections = {
            "resident": _conn("resident", enabled=True, tools=[_FakeTool("a")]),
            "temp": _conn("temp", enabled=False, tools=[_FakeTool("b")]),
        }
        mgr._ephemeral = {"temp"}

        by_server = mgr.get_tool_schemas_by_server()

        assert "resident" in by_server
        assert "temp" not in by_server, "临时开启的服务器不得进入常驻工具列表（token 泄露）"
        names = [s["function"]["name"] for s in mgr.get_tool_schemas()]
        assert names == ["mcp__resident__a"]


class TestEnableEphemeral:
    def test_enable_returns_tool_schemas(self, mgr, patch_pm):
        """成功临时开启 → 返回该服务器的工具定义（供 tool_execute 中转）"""
        cfg = {"name": "temp", "enabled": False, "command": "x", "args": []}
        patch_pm([cfg])

        async def _fake_connect(name, config):
            mgr._connections[name] = _conn(name, enabled=False, tools=[_FakeTool("q")])
            return True, ""

        mgr._connect_single = _fake_connect
        ok, payload = mgr.enable_ephemeral("temp")

        assert ok is True
        assert [s["function"]["name"] for s in payload] == ["mcp__temp__q"]
        assert mgr.ephemeral_names() == {"temp"}

    def test_enable_rejects_resident_server(self, mgr, patch_pm):
        """已常驻开启的服务器无需临时开启（工具已在列表里）"""
        patch_pm([{"name": "res", "enabled": True}])
        mgr._connections = {"res": _conn("res", enabled=True, tools=[_FakeTool("t")])}

        ok, err = mgr.enable_ephemeral("res")

        assert ok is False
        assert "常驻" in str(err)
        assert mgr.ephemeral_names() == set()

    def test_enable_unknown_server_reports_list_hint(self, mgr, patch_pm):
        patch_pm([{"name": "other", "enabled": False}])
        ok, err = mgr.enable_ephemeral("nope")
        assert ok is False
        assert "未找到" in str(err)

    def test_enable_failure_clears_ephemeral_mark(self, mgr, patch_pm):
        """连接失败必须撤回临时标记，否则会留下永远清不掉的幽灵条目"""
        patch_pm([{"name": "temp", "enabled": False}])

        async def _fake_connect(name, config):
            return False, "boom"

        mgr._connect_single = _fake_connect
        ok, err = mgr.enable_ephemeral("temp")

        assert ok is False
        assert "boom" in str(err)
        assert mgr.ephemeral_names() == set()
        assert "temp" not in mgr._busy_names, "失败后必须释放防重锁，否则该服务器永久不可再开"

    def test_enable_need_confirm_gives_actionable_hint(self, mgr, patch_pm):
        """门禁要确认时，错误信息要指向设置页（模型不能弹窗打断对话）"""
        patch_pm([{"name": "temp", "enabled": False}])

        async def _fake_connect(name, config):
            return False, "连接失败: 启动被安全门禁拦截（need_confirm）"

        mgr._connect_single = _fake_connect
        ok, err = mgr.enable_ephemeral("temp")

        assert ok is False
        assert "设置" in str(err) and "MCP" in str(err)


class TestDisableEphemeral:
    def test_disable_only_affects_ephemeral(self, mgr):
        """常驻开启的服务器不能被模型关掉（防止误改用户配置）"""
        mgr._connections = {"res": _conn("res", enabled=True)}
        ok, err = mgr.disable_ephemeral("res")
        assert ok is False
        assert "设置" in err

    def test_disable_clears_mark_and_disconnects(self, mgr):
        mgr._connections = {"temp": _conn("temp", enabled=False)}
        mgr._ephemeral = {"temp"}
        called = []

        async def _fake_disconnect(name, *, keep_record=False):
            called.append((name, keep_record))
            mgr._connections[name].set_state(MCPState.DISABLED)
            mgr._connections[name].session = None
            return True

        mgr._disconnect_single = _fake_disconnect
        ok, err = mgr.disable_ephemeral("temp")

        assert ok is True and err == ""
        assert called == [("temp", True)]
        assert mgr.ephemeral_names() == set()


# ═══════════════════════════════════════════════════════════
# 2. 孤儿清理豁免（临时开启必须活得过全量连接 / 热重载）
# ═══════════════════════════════════════════════════════════


class TestOrphanCleanupExemption:
    def test_connect_all_does_not_kill_ephemeral(self, mgr):
        """_connect_all 的 stale 清理不得把临时开启当孤儿断开"""
        mgr._connections = {
            "resident": _conn("resident", enabled=True),
            "temp": _conn("temp", enabled=False),
        }
        mgr._ephemeral = {"temp"}
        killed = []

        async def _fake_disconnect(name, *, keep_record=False):
            killed.append(name)
            return True

        async def _fake_connect(name, config):
            return True, ""

        mgr._disconnect_single = _fake_disconnect
        mgr._connect_single = _fake_connect
        # 目标配置只含 resident（temp 在配置里仍是 enabled=false）
        asyncio.run(mgr._connect_all([{"name": "resident", "enabled": True}]))

        assert "temp" not in killed, "临时开启的服务器被全量连接当孤儿杀掉了"
        assert mgr.ephemeral_names() == {"temp"}

    def test_connect_all_promotes_newly_enabled_server(self, mgr):
        """临时开着的同时用户把它在配置里打开 → 转为常驻，不再算临时"""
        mgr._connections = {"temp": _conn("temp", enabled=True)}
        mgr._ephemeral = {"temp"}

        async def _fake_connect(name, config):
            return True, ""

        mgr._connect_single = _fake_connect
        asyncio.run(mgr._connect_all([{"name": "temp", "enabled": True}]))

        assert mgr.ephemeral_names() == set()

    def test_disconnect_missing_skips_ephemeral(self, mgr):
        """热重载按 enabled 名单清孤儿时，临时开启的必须留活"""
        mgr._connections = {
            "keep": _conn("keep", enabled=True),
            "temp": _conn("temp", enabled=False),
            "orphan": _conn("orphan", enabled=True),
        }
        mgr._ephemeral = {"temp"}
        calls = []
        mgr.disconnect_server_background = lambda name, on_done=None: calls.append(name)

        mgr.disconnect_missing({"keep"})

        assert set(calls) == {"orphan"}
        assert mgr.ephemeral_names() == {"temp"}


# ═══════════════════════════════════════════════════════════
# 3. 幽灵状态清理
# ═══════════════════════════════════════════════════════════


class TestNoGhostState:
    def test_manual_disconnect_clears_mark(self, mgr):
        """用户在设置里关掉临时开启的服务器 → 标记必须一起清掉"""
        mgr._connections = {"temp": _conn("temp", enabled=False)}
        mgr._ephemeral = {"temp"}
        started = []

        class _FakeThread:
            def __init__(self, *a, **kw):
                pass

            def start(self):
                started.append(True)

        import app.tools.mcp_tools as mod

        orig = mod.threading.Thread
        mod.threading.Thread = _FakeThread
        try:
            mgr.disconnect_server_background("temp")
        finally:
            mod.threading.Thread = orig

        assert started == [True]
        assert mgr.ephemeral_names() == set()

    def test_disconnect_all_clears_marks(self, mgr):
        mgr._connections = {"temp": _conn("temp", enabled=False)}
        mgr._ephemeral = {"temp"}

        async def _fake_disconnect(name, *, keep_record=True):
            return True

        mgr._disconnect_single = _fake_disconnect
        asyncio.run(mgr._disconnect_all())

        assert mgr.ephemeral_names() == set()

    def test_status_reports_ephemeral_flag(self, mgr):
        mgr._connections = {
            "resident": _conn("resident", enabled=True),
            "temp": _conn("temp", enabled=False),
            "off": _conn("off", enabled=False, state=MCPState.DISABLED),
        }
        mgr._ephemeral = {"temp"}

        status = {s["name"]: s for s in mgr.get_status()}

        assert status["resident"]["ephemeral"] is False
        assert status["temp"]["ephemeral"] is True
        assert status["off"]["ephemeral"] is False
