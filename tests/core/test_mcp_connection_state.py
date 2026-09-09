# -*- coding: utf-8 -*-
"""MCP 链路回归测试：四态状态机 + 死锁 / 连接踩踏修复

背景（根因）：
1. `_disconnect_single` 在持有 `self._lock`（threading.Lock）时 `await`，
   事件循环线程持锁挂起期间，其它协程 / UI 线程一旦 `with self._lock`
   就会阻塞住整个事件循环线程 → 锁永不释放 → MCP 全部起不来且设置页卡死。
2. `_connect_all` 开头无条件 `_disconnect_all()`，多窗口各调一次时后启动的
   窗口会把前一个窗口刚连好的连接全部拆掉（连接踩踏）。
3. 连接失败 / 连接中的 server 从不进入 `_connections`，`get_status()` 因此
   永远读不到 CONNECTING / FAILED，UI 只能显示"未连接"灰点。
4. stdio 子进程未继承完整父进程环境，丢失 HTTP_PROXY 等变量导致拉包失败。

本测试锁定修复后的行为。
"""

import asyncio
import threading

import pytest

from app.tools.mcp_tools import (
    MCPClientManager,
    MCPServerConnection,
    MCPState,
    _build_stdio_env,
    _resolve_plugin_paths,
)


@pytest.fixture
def mgr():
    """构造一个不启动真实事件循环线程的 manager"""
    m = MCPClientManager.__new__(MCPClientManager)
    m._connections = {}
    m._connected = False
    m._busy_names = set()
    m._busy_lock = threading.Lock()
    m._connect_all_running = False
    m._lock = threading.Lock()
    m._loop = None
    m._thread = None
    return m


def _make_conn(name, state=MCPState.CONNECTED, config=None):
    conn = MCPServerConnection(name, config or {"name": name, "enabled": True})
    conn.set_state(state)
    if state == MCPState.CONNECTED:
        conn.session = object()  # 占位，仅用于 connected 判定
    return conn


# ═══════════════════════════════════════════════════════════
# 0. 启动前占位符兜底解析（防 ${CLAUDE_PLUGIN_ROOT} 字面量直达子进程）
# ═══════════════════════════════════════════════════════════


class TestResolvePluginPaths:
    _SOURCE = "D:/work/DriFox/.drifox/plugins/browser/.mcp.json"

    def test_expands_root_placeholder(self):
        cfg = {
            "command": "py",
            "args": ["-3", "${CLAUDE_PLUGIN_ROOT}/mcp/server.py"],
            "env": {},
            "url": "",
            "headers": {},
            "_source": self._SOURCE,
        }
        out = _resolve_plugin_paths(cfg)
        assert out["args"][1] == "D:/work/DriFox/.drifox/plugins/browser/mcp/server.py"

    def test_expands_data_placeholder(self):
        cfg = {"args": [], "env": {"X": "${CLAUDE_PLUGIN_ROOT}/data"}, "_source": self._SOURCE}
        out = _resolve_plugin_paths(cfg)
        assert out["env"]["X"] == "D:/work/DriFox/.drifox/plugins/browser/data"

    def test_idempotent_on_already_expanded(self):
        cfg = {"args": ["D:/work/DriFox/.drifox/plugins/browser/mcp/server.py"], "_source": self._SOURCE}
        # 已展开的绝对值不应被二次改变
        assert _resolve_plugin_paths(cfg)["args"] == cfg["args"]

    def test_no_source_is_passthrough(self):
        cfg = {"args": ["${CLAUDE_PLUGIN_ROOT}/mcp/server.py"]}
        # 无 _source 时原样返回，不影响普通配置
        assert _resolve_plugin_paths(cfg) is cfg

    def test_does_not_mutate_original(self):
        cfg = {"args": ["${CLAUDE_PLUGIN_ROOT}/mcp/server.py"], "_source": self._SOURCE}
        _resolve_plugin_paths(cfg)
        assert cfg["args"][0] == "${CLAUDE_PLUGIN_ROOT}/mcp/server.py"


# ═══════════════════════════════════════════════════════════
# 1. 状态机 / get_status
# ═══════════════════════════════════════════════════════════


class TestStatusReporting:
    def test_status_includes_failed_and_connecting(self, mgr):
        """失败态与启动中都必须出现在 get_status 里（旧实现只返回成功的）"""
        mgr._connections = {
            "ok": _make_conn("ok", MCPState.CONNECTED),
            "bad": _make_conn("bad", MCPState.FAILED),
            "starting": _make_conn("starting", MCPState.CONNECTING),
            "off": _make_conn("off", MCPState.DISABLED),
        }
        mgr._connections["bad"].set_state(MCPState.FAILED, "boom")

        status = {s["name"]: s for s in mgr.get_status()}

        assert set(status) == {"ok", "bad", "starting", "off"}
        assert status["ok"]["state"] == MCPState.CONNECTED
        assert status["ok"]["connected"] is True
        assert status["bad"]["state"] == MCPState.FAILED
        assert status["bad"]["error"] == "boom"
        assert status["starting"]["state"] == MCPState.CONNECTING
        assert status["off"]["state"] == MCPState.DISABLED

    def test_busy_name_forces_connecting(self, mgr):
        """busy 集合中的 server 即便记录是旧的 FAILED，也应报告为启动中"""
        mgr._connections = {"srv": _make_conn("srv", MCPState.FAILED)}
        mgr._busy_names = {"srv"}

        st = mgr.get_status()[0]
        assert st["state"] == MCPState.CONNECTING
        assert st["busy"] is True

    def test_connected_requires_both_session_and_state(self, mgr):
        """session 被清掉后不得再报告 connected"""
        conn = _make_conn("srv", MCPState.CONNECTED)
        conn.session = None
        mgr._connections = {"srv": conn}

        assert mgr.get_status()[0]["connected"] is False


# ═══════════════════════════════════════════════════════════
# 2. 死锁修复：_disconnect_single 不得在持锁时 await
# ═══════════════════════════════════════════════════════════


class TestDisconnectNoDeadlock:
    def test_lock_is_released_before_await(self, mgr):
        """断开过程中另一个线程必须能立刻拿到 _lock

        旧实现在 `with self._lock:` 块里 await，事件循环挂起时锁仍被持有，
        其它线程 acquire 会阻塞 → 死锁。
        """
        acquired = threading.Event()

        async def _slow_lifespan():
            # 模拟生命周期 Task 退出缓慢（原实现会在此期间一直持锁）
            await asyncio.sleep(0.3)

        async def _scenario():
            conn = _make_conn("srv")
            conn._disconnect_event = asyncio.Event()
            conn._task = asyncio.ensure_future(_slow_lifespan())
            mgr._connections = {"srv": conn}

            def _other_thread():
                # 断开进行中尝试抢锁，必须能迅速拿到
                with mgr._lock:
                    acquired.set()

            t = threading.Thread(target=_other_thread, daemon=True)

            task = asyncio.ensure_future(mgr._disconnect_single("srv"))
            await asyncio.sleep(0.05)  # 让 _disconnect_single 进入 await 阶段
            t.start()
            t.join(timeout=2)
            await task

        asyncio.run(asyncio.wait_for(_scenario(), timeout=5))
        assert acquired.is_set(), "断开期间 _lock 未及时释放 —— 存在持锁 await 死锁"

    def test_keep_record_marks_disabled(self, mgr):
        """用户主动关闭：保留记录并置 DISABLED（供 UI 显示黑色）"""

        async def _scenario():
            conn = _make_conn("srv")
            conn._disconnect_event = asyncio.Event()
            conn._task = None
            mgr._connections = {"srv": conn}
            await mgr._disconnect_single("srv", keep_record=True)

        asyncio.run(_scenario())

        assert "srv" in mgr._connections
        assert mgr._connections["srv"].state == MCPState.DISABLED
        assert mgr._connections["srv"].session is None

    def test_default_removes_record(self, mgr):
        async def _scenario():
            conn = _make_conn("srv")
            conn._disconnect_event = asyncio.Event()
            conn._task = None
            mgr._connections = {"srv": conn}
            await mgr._disconnect_single("srv")

        asyncio.run(_scenario())
        assert "srv" not in mgr._connections

    def test_disconnect_missing_server_returns_false(self, mgr):
        assert asyncio.run(mgr._disconnect_single("nope")) is False


# ═══════════════════════════════════════════════════════════
# 3. 连接踩踏修复：_connect_all 幂等
# ═══════════════════════════════════════════════════════════


class TestConnectAllIdempotent:
    def test_skips_already_connected_same_config(self, mgr):
        """已连接且配置未变 → 不重连（旧实现会先全断再全连）"""
        cfg = {"name": "srv", "enabled": True, "command": "x"}
        mgr._connections = {"srv": _make_conn("srv", MCPState.CONNECTED, cfg)}

        calls = []

        async def _fake_connect(name, config):
            calls.append(name)
            return True, ""

        mgr._connect_single = _fake_connect
        asyncio.run(mgr._connect_all([cfg]))

        assert calls == [], "已连接且配置未变的 server 不应重连"
        assert mgr._connected is True

    def test_reconnects_when_config_changed(self, mgr):
        old = {"name": "srv", "enabled": True, "command": "old"}
        new = {"name": "srv", "enabled": True, "command": "new"}
        mgr._connections = {"srv": _make_conn("srv", MCPState.CONNECTED, old)}

        calls = []

        async def _fake_connect(name, config):
            calls.append((name, config["command"]))
            mgr._connections[name] = _make_conn(name, MCPState.CONNECTED, config)
            return True, ""

        mgr._connect_single = _fake_connect
        asyncio.run(mgr._connect_all([new]))

        assert calls == [("srv", "new")]

    def test_retries_previously_failed(self, mgr):
        """上次失败的 server 下一轮应重试"""
        cfg = {"name": "srv", "enabled": True}
        mgr._connections = {"srv": _make_conn("srv", MCPState.FAILED, cfg)}

        calls = []

        async def _fake_connect(name, config):
            calls.append(name)
            return False, "still bad"

        mgr._connect_single = _fake_connect
        asyncio.run(mgr._connect_all([cfg]))

        assert calls == ["srv"]

    def test_disabled_server_is_disconnected_not_connected(self, mgr):
        """目标配置里被禁用的 server 应被断开，且不会尝试连接"""
        cfg_on = {"name": "srv", "enabled": True}
        mgr._connections = {"srv": _make_conn("srv", MCPState.CONNECTED, cfg_on)}

        disconnected = []

        async def _fake_disconnect(name, *, keep_record=False):
            disconnected.append(name)
            mgr._connections[name].set_state(MCPState.DISABLED)
            mgr._connections[name].session = None
            return True

        async def _fake_connect(name, config):
            raise AssertionError("禁用的 server 不应被连接")

        mgr._disconnect_single = _fake_disconnect
        mgr._connect_single = _fake_connect
        asyncio.run(mgr._connect_all([{"name": "srv", "enabled": False}]))

        assert disconnected == ["srv"]
        assert mgr._connected is False

    def test_no_full_disconnect_at_start(self, mgr):
        """开头绝不能无条件全断（多窗口踩踏根因）"""
        called = []

        async def _fake_disconnect_all(*a, **kw):
            called.append(True)

        mgr._disconnect_all = _fake_disconnect_all

        async def _fake_connect(name, config):
            mgr._connections[name] = _make_conn(name, MCPState.CONNECTED, config)
            return True, ""

        mgr._connect_single = _fake_connect
        mgr._connected = True
        asyncio.run(mgr._connect_all([{"name": "a", "enabled": True}]))

        assert called == [], "_connect_all 不应调用 _disconnect_all"


# ═══════════════════════════════════════════════════════════
# 4. connect_all_background 全局去重
# ═══════════════════════════════════════════════════════════


class TestConnectAllDedup:
    def test_second_call_is_skipped_while_running(self, mgr):
        """已有一轮全量连接进行中时，第二次调用直接跳过"""
        mgr._connect_all_running = True
        done = []

        mgr.connect_all_background(
            [{"name": "a", "enabled": True}],
            on_done=lambda ok, total, failed: done.append((ok, total, failed)),
        )

        # 未启动新线程，直接走 on_done(0, total, [])
        assert done == [(0, 1, [])]


# ═══════════════════════════════════════════════════════════
# 5. 子进程环境变量继承
# ═══════════════════════════════════════════════════════════


class TestStdioEnv:
    def test_inherits_parent_environment(self, monkeypatch):
        """必须继承完整父进程环境（代理/证书/镜像源），否则 npx 拉包失败"""
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
        env = _build_stdio_env(None)
        assert env.get("HTTPS_PROXY") == "http://127.0.0.1:7890"
        assert "PATH" in env

    def test_user_env_overrides_parent(self, monkeypatch):
        monkeypatch.setenv("FOO", "parent")
        env = _build_stdio_env({"FOO": "child"})
        assert env["FOO"] == "child"

    def test_skips_exported_shell_functions(self, monkeypatch):
        monkeypatch.setenv("BAD_FN", "() { echo hi; }")
        env = _build_stdio_env(None)
        assert "BAD_FN" not in env

    def test_none_values_dropped_and_stringified(self):
        env = _build_stdio_env({"A": None, "B": 123})
        assert "A" not in env
        assert env["B"] == "123"


class TestDisconnectMissing:
    """热重载后断开已失效的连接（插件删除 / 服务器移除 / 禁用）"""

    def test_disconnects_orphans_only(self, mgr):
        """不在 valid_names 中的已连服务器应被断开，在列表中的保持不变"""
        mgr._connections = {
            "keep1": _make_conn("keep1", MCPState.CONNECTED),
            "keep2": _make_conn("keep2", MCPState.CONNECTING),
            "gone_plugin": _make_conn("gone_plugin", MCPState.CONNECTED),
            "removed": _make_conn("removed", MCPState.FAILED),
        }
        # 用 mock 替换后台断开，避免真实线程 / 事件循环依赖
        calls = []
        mgr.disconnect_server_background = lambda name, on_done=None: calls.append(name)

        mgr.disconnect_missing({"keep1", "keep2"})

        assert set(calls) == {"gone_plugin", "removed"}
        # 列表内的连接不应被触碰
        assert "keep1" in mgr._connections and "keep2" in mgr._connections

    def test_no_orphans_is_noop(self, mgr):
        mgr._connections = {"a": _make_conn("a", MCPState.CONNECTED)}
        calls = []
        mgr.disconnect_server_background = lambda name, on_done=None: calls.append(name)
        mgr.disconnect_missing({"a"})
        assert calls == []

    def test_disconnects_all_when_no_valid(self, mgr):
        """全局关闭（valid 为空）时，所有连接都应被断开"""
        mgr._connections = {
            "x": _make_conn("x", MCPState.CONNECTED),
            "y": _make_conn("y", MCPState.FAILED),
        }
        calls = []
        mgr.disconnect_server_background = lambda name, on_done=None: calls.append(name)
        mgr.disconnect_missing(set())
        assert set(calls) == {"x", "y"}
