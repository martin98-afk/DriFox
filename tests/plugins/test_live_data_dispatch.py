# -*- coding: utf-8 -*-
"""live-data 插件工具分发回归测试。

历史 bug：四个 live_* 工具共用 _impl，靠 getattr(tool_ctx, "tool_name") 区分，
但宿主注入的 tool_ctx 是 dict 且不含工具名（也不注入 __tool__ 键）→
工具名恒为空串 → 一律落入「未知工具：」兜底，插件从未真正执行过。
回归锁：按宿主真实调用方式 impl(tool_ctx=ctx, **args)，四个工具名分发正确，
kind/city/target 参数透传无误；网络底层全部打桩，不触网。
"""
import importlib.util
import os
import sys
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from app.tools.registry import ToolRegistry


def _load_tool():
    p = _ROOT / ".drifox" / "plugins" / "live-data" / "tools" / "live_data.py"
    spec = importlib.util.spec_from_file_location("live_data_under_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod(monkeypatch):
    """加载 live_data 模块并把网络底层换为记录桩。"""
    m = _load_tool()
    calls = []

    def _rec(name, *args):
        calls.append((name, *args))
        return f"stub:{name}"

    monkeypatch.setattr(m, "_finance", lambda kind, kw: _rec("finance_live", kind, dict(kw)))
    monkeypatch.setattr(m, "_dev", lambda kind, target: _rec("dev_live", kind, target))
    monkeypatch.setattr(m, "_weather_impl", lambda city: _rec("weather_live", city))
    monkeypatch.setattr(m, "_bili", lambda target: _rec("misc_live", "bilibili", target))
    monkeypatch.setattr(m, "_ip", lambda target: _rec("misc_live", "ip", target))
    m._calls = calls
    return m


# ------------------------------------------------ 第一层：插件内部分发 seam

def test_finance_dispatch(mod):
    r = mod._finance_live_impl(tool_ctx={}, kind="指数")
    assert r.success and r.content == "stub:finance_live"
    assert mod._calls == [("finance_live", "指数", {"kind": "指数"})]


def test_dev_dispatch(mod):
    r = mod._dev_live_impl(tool_ctx=None, kind="github", target="martin98-afk/DriFox")
    assert r.success and r.content == "stub:dev_live"
    assert mod._calls == [("dev_live", "github", "martin98-afk/DriFox")]


def test_weather_dispatch(mod):
    r = mod._weather_live_impl(tool_ctx=None, city="北京")
    assert r.success and r.content == "stub:weather_live"
    assert mod._calls == [("weather_live", "北京")]


def test_misc_bilibili_dispatch(mod):
    r = mod._misc_live_impl(tool_ctx=None, kind="bilibili", target="BV1uNk1YxEJQ")
    assert r.success
    assert mod._calls == [("misc_live", "bilibili", "BV1uNk1YxEJQ")]


def test_misc_ip_dispatch(mod):
    r = mod._misc_live_impl(tool_ctx=None, kind="ip")
    assert r.success
    assert mod._calls == [("misc_live", "ip", "")]


def test_invalid_kind_rejected_without_network(mod):
    r = mod._finance_live_impl(tool_ctx=None, kind="期货")
    assert not r.success
    assert "kind 必须" in r.error
    assert mod._calls == []


# ------------------------------------------------ 第二层：ToolExecutor 端到端

class _FakeMcpManager:
    def __init__(self):
        self.is_connected = False
        self._connections = {}


class _FakeBuiltinTools:
    """ToolExecutor.is_valid/_try_recover_mcp_prefix 所需的最小编成对象。"""

    def __init__(self):
        self.workdir = "/wd"
        self._mcp_manager = _FakeMcpManager()
        self._lsp_tools = None
        self.gitee_upload = None
        self._team_window_id = ""
        self._team_agent_name = ""


def _new_executor():
    from app.core.tools.tool_executor import ToolExecutor

    ex = object.__new__(ToolExecutor)
    ex._builtin_tools = _FakeBuiltinTools()
    ex._backend = None
    ex._lock = threading.Lock()
    ex._session_id = "sess-live"
    ex._call_id = "call-live"
    ex._workdir = "/wd"
    ex._workdir_user_set = True
    ex._sub_agent_manager = None
    ex._window_state = {}
    ex._window_state_lock = threading.Lock()
    ex._file_recorder = None
    return ex


@pytest.fixture()
def fresh_registry():
    ToolRegistry.reset_instance()
    yield
    ToolRegistry.reset_instance()


def test_executor_end_to_end_dispatch(mod, fresh_registry):
    """宿主链路 ToolExecutor.execute → 包装 impl → 正确分发（复现原 bug 的真实调用方式）。"""
    # 走生产注册代理（强制注入 source=plugin:live-data，裸 registry 会拒绝无 source 注册）
    from app.plugins.loaders.plugin_tool_loader import _PluginRegistryProxy

    proxy = _PluginRegistryProxy(ToolRegistry.get_instance(), "live-data")
    mod.register(proxy)
    ex = _new_executor()
    r = ex.execute("finance_live", {"kind": "金价"})
    assert r.success, f"端到端执行失败: {r.error}"
    assert r.content == "stub:finance_live"
    assert mod._calls == [("finance_live", "金价", {"kind": "金价"})]
