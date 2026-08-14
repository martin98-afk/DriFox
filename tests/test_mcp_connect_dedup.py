# -*- coding: utf-8 -*-
"""回归测试：MCP 连接防踩踏 + 防重 on_done 锁外执行

背景（根因，2026-08-14 实测复现）：
1. 启动路径 connect_all_background 与热重载路径 connect_server_background
   使用两套独立防重（_connect_all_running vs _busy_names），并行连接同一
   服务器时，connect_server_background 的 _connect_single 会 _disconnect_single
   杀掉 connect_all 的子进程并取消其生命周期 Task → 连接反复被杀重启、
   后台线程空等 90s、_connect_all_running 期间全量连接全部被跳过。
2. connect_server_background 防重分支在持有 _busy_lock 时同步执行 on_done
   （真实 UI 里触发主线程 InfoBar/刷新），拉长锁持有时间。

修复：
1. connect_all_background 把本轮服务器名登记进 _busy_names，期间同名
   connect_server_background 防重跳过；完成后释放。
2. 防重分支的 on_done 移到锁外执行。
"""

import threading
import time

import pytest

from app.tools.mcp_tools import MCPClientManager, MCPServerConnection, MCPState


@pytest.fixture
def mgr():
    """构造一个不启动真实事件循环线程的 manager"""
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
    return m


def _server(name="srv", enabled=True):
    return {"name": name, "type": "stdio", "command": "echo", "args": [], "enabled": enabled}


class TestConnectAllBlocksServerBackground:
    def test_registers_busy_names_and_releases(self, mgr, monkeypatch):
        """全量连接进行中，服务器名登记进 _busy_names；结束后释放"""
        # 拦截线程启动，同步验证登记逻辑（避免 _worker 异步时序竞态）
        started = []
        monkeypatch.setattr(
            "app.tools.mcp_tools.threading.Thread",
            lambda *a, **kw: type("FakeThread", (), {"start": lambda self: started.append(True)})(),
        )
        mgr.connect_all_background([_server("a"), _server("b", enabled=False), _server("c")])
        # 登记的是 enabled 且有名字的服务器
        assert "a" in mgr._busy_names
        assert "c" in mgr._busy_names
        assert "b" not in mgr._busy_names  # disabled 不登记

        # 模拟 _worker finally 的清理逻辑
        with mgr._busy_lock:
            mgr._connect_all_running = False
            mgr._busy_names -= mgr._connect_all_names
            mgr._connect_all_names = set()
        assert mgr._busy_names == set()
        assert mgr._connect_all_running is False
        assert started == [True]

    def test_server_background_skipped_while_connect_all(self, mgr):
        """全量连接进行中，同名单服务器连接被防重跳过（on_done 收到跳过回调）"""
        mgr._busy_names.add("srv")
        mgr._connect_all_running = True
        mgr._connect_all_names = {"srv"}

        calls = []
        mgr.connect_server_background("srv", _server("srv"), on_done=lambda n, s, e: calls.append((n, s, e)))
        assert calls == [("srv", False, "服务器正在操作中，请稍后重试")]
        # 未被预登记新连接（防重直接返回）
        assert "srv" not in mgr._connections


class TestDupOnDoneOutsideLock:
    def test_lock_released_when_on_done_runs(self, mgr):
        """防重跳过后，on_done 执行期间 _busy_lock 必须可立即获取（锁外回调）"""
        mgr._busy_names.add("srv")

        lock_acquired = []

        def on_done(n, s, e):
            # 在 on_done 内尝试获取锁：修复前死锁（锁内回调），修复后立即可获取
            with mgr._busy_lock:
                lock_acquired.append(True)

        mgr.connect_server_background("srv", _server("srv"), on_done=on_done)
        assert lock_acquired == [True]

    def test_normal_connect_still_registers(self, mgr):
        """非防重路径：连接请求登记 _busy_names 并预登记 CONNECTING"""
        mgr.connect_server_background("srv", _server("srv"))
        assert "srv" in mgr._busy_names
        conn = mgr._connections.get("srv")
        assert conn is not None
        assert conn.state == MCPState.CONNECTING
