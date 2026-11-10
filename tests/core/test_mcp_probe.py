# -*- coding: utf-8 -*-
"""MCP 授权探活：判定口径 / 状态合并 / TTL / stdio 跳过

背景：MCP 服务只有「连上过」才有状态，enabled=false 的服务在连接表里
根本没有条目，设置卡一律显示「已关闭」——包括 token 早已过期的 OAuth 服务。
本测试锁定探活与状态合并的最小契约。
"""

import asyncio
import threading
import time

import httpx
import pytest

from app.core.mcp_oauth.provider import probe_authorized
from app.tools.mcp_tools import MCPClientManager, MCPServerConnection, MCPState

# ═══════════════════════════════════════════════════════════
# 0. 造桩
# ═══════════════════════════════════════════════════════════


def _bare_manager(cfg_map):
    """不启动事件循环线程的 manager（沿用 test_mcp_connection_state 的写法）。"""
    m = MCPClientManager.__new__(MCPClientManager)
    m._connections = {}
    m._connected = False
    m._busy_names = set()
    m._busy_lock = threading.Lock()
    m._connect_all_running = False
    m._lock = threading.Lock()
    m._loop = None
    m._thread = None
    m._ephemeral = set()
    m._probes = {}
    m._probe_last_at = 0.0
    m._probe_thread = None
    m._configured_servers = lambda: list(cfg_map.values())
    return m


def _make_conn(name, config=None):
    conn = MCPServerConnection(name, config or {"name": name, "enabled": True})
    conn.set_state(MCPState.CONNECTED)
    conn.session = object()  # 占位，仅用于 connected 判定
    return conn


class _Ctx:
    def __init__(self, url):
        self.server_url = url
        self.redirect_handler = None
        self.callback_handler = None
        self.client_metadata = None


class _Provider:
    """最小假 provider：首请求按 _status 回，401 时走授权分支。"""

    def __init__(self, url, first_status):
        self.context = _Ctx(url)
        self._status = first_status

    def async_auth_flow(self, request):
        async def flow():
            response = yield request
            if response.status_code == self._status and self.context.redirect_handler:
                await self.context.redirect_handler("http://auth")

        return flow()


def _patch_httpx(monkeypatch, status):
    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, request):
            return httpx.Response(status, request=request)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


# ═══════════════════════════════════════════════════════════
# 1. 本地过期判定（零网络）
# ═══════════════════════════════════════════════════════════


def _store_with(blob):
    """构造一个只读的 token store（假 PluginConfigStore，不碰磁盘）。"""
    from app.core.mcp_oauth.token_store import PluginMcpTokenStore, oauth_storage_key

    st = PluginMcpTokenStore.__new__(PluginMcpTokenStore)
    st._plugin_name = "plug"
    st._server_name = "srv"
    st._key = oauth_storage_key("srv")
    st._store = type("S", (), {"_read_raw": lambda self, name: {st._key: blob}})()
    return st


def test_peek_expiry_no_token():
    assert _store_with({}).peek_expiry() == (False, None, False)


def test_peek_expiry_expired_without_refresh():
    blob = {
        "token": {"access_token": "a", "token_type": "Bearer"},
        "expires_at": time.time() - 60,
    }
    has_token, expires_at, has_refresh = _store_with(blob).peek_expiry()
    assert has_token is True
    assert expires_at == pytest.approx(blob["expires_at"])
    assert has_refresh is False


def test_peek_expiry_with_refresh():
    blob = {
        "token": {"access_token": "a", "token_type": "Bearer", "refresh_token": "r"},
        "expires_at": time.time() + 3600,
    }
    _, _, has_refresh = _store_with(blob).peek_expiry()
    assert has_refresh is True


# ═══════════════════════════════════════════════════════════
# 2. OAuth 探活判定
# ═══════════════════════════════════════════════════════════


def test_probe_ok(monkeypatch):
    _patch_httpx(monkeypatch, 200)
    state, _ = asyncio.run(probe_authorized("http://x/mcp", _Provider("http://x/mcp", 200)))
    assert state == "ok"


def test_probe_auth_expired(monkeypatch):
    _patch_httpx(monkeypatch, 401)
    state, detail = asyncio.run(probe_authorized("http://x/mcp", _Provider("http://x/mcp", 401)))
    assert state == "auth_expired"
    assert detail


def test_probe_unreachable(monkeypatch):
    _patch_httpx(monkeypatch, 500)
    state, detail = asyncio.run(probe_authorized("http://x/mcp", _Provider("http://x/mcp", 500)))
    assert state == "unreachable"
    assert "500" in detail


def test_probe_restores_handlers(monkeypatch):
    _patch_httpx(monkeypatch, 200)
    provider = _Provider("http://x/mcp", 200)
    asyncio.run(probe_authorized("http://x/mcp", provider))
    assert provider.context.redirect_handler is None
    assert provider.context.callback_handler is None


# ═══════════════════════════════════════════════════════════
# 3. 状态合并
# ═══════════════════════════════════════════════════════════


def test_status_merge_auth_expired_beats_disabled():
    m = _bare_manager(
        {
            "figma": {
                "name": "figma",
                "type": "http",
                "enabled": False,
                "url": "https://mcp.figma.com/mcp",
                "auth": {"type": "oauth"},
            }
        }
    )
    m._probes["figma"] = {"state": "auth_expired", "detail": "授权已过期", "probed_at": 1.0}
    item = m.get_status()[0]
    assert item["state"] == MCPState.AUTH_EXPIRED
    assert item["probe_state"] == "auth_expired"
    assert item["error"] == "授权已过期"


def test_status_connected_wins_over_probe():
    m = _bare_manager({})
    m._connections["bili"] = _make_conn("bili")
    m._probes["bili"] = {"state": "unreachable", "detail": "timeout", "probed_at": 1.0}
    item = m.get_status()[0]
    assert item["state"] == MCPState.CONNECTED


def test_status_unreachable_disabled_stays_disabled():
    m = _bare_manager({"notion": {"name": "notion", "type": "http", "enabled": False}})
    m._probes["notion"] = {"state": "unreachable", "detail": "timeout", "probed_at": 1.0}
    assert m.get_status()[0]["state"] == MCPState.DISABLED


def test_status_unreachable_enabled_is_failed():
    m = _bare_manager({"zhipu": {"name": "zhipu", "type": "http", "enabled": True}})
    m._probes["zhipu"] = {"state": "unreachable", "detail": "timeout", "probed_at": 1.0}
    item = m.get_status()[0]
    assert item["state"] == MCPState.FAILED
    assert item["error"] == "timeout"


# ═══════════════════════════════════════════════════════════
# 4. 探活批次：stdio 跳过 / TTL / busy 跳过
# ═══════════════════════════════════════════════════════════


def test_probe_skips_stdio(monkeypatch):
    m = _bare_manager(
        {
            "ex": {"name": "ex", "type": "stdio", "enabled": True, "command": "npx"},
            "figma": {
                "name": "figma",
                "type": "http",
                "enabled": False,
                "url": "https://mcp.figma.com/mcp",
            },
        }
    )
    seen = []

    async def fake_probe(cfg):
        seen.append(cfg["name"])
        return "ok", ""

    monkeypatch.setattr(m, "_probe_one", fake_probe)
    asyncio.run(m._probe_all())
    assert seen == ["figma"]


def test_probe_ttl_skips_second_run(monkeypatch):
    m = _bare_manager({})
    calls = []
    gate = threading.Event()

    def fake_run(coro, timeout=None):
        calls.append(1)
        gate.wait(2)
        coro.close()
        return None

    monkeypatch.setattr(m, "_run_async", fake_run)
    monkeypatch.setattr(m, "_probe_all", lambda: asyncio.sleep(0))
    m.probe_servers_background()
    m.probe_servers_background()
    gate.set()
    m._probe_thread.join(3)
    assert len(calls) == 1


def test_probe_skips_busy_server(monkeypatch):
    m = _bare_manager({"figma": {"name": "figma", "type": "http", "enabled": True,
                                 "url": "https://mcp.figma.com/mcp"}})
    m._busy_names.add("figma")
    seen = []

    async def fake_probe(cfg):
        seen.append(cfg["name"])
        return "ok", ""

    monkeypatch.setattr(m, "_probe_one", fake_probe)
    asyncio.run(m._probe_all())
    assert seen == []
    assert m._probes == {}
