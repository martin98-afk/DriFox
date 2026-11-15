# -*- coding: utf-8 -*-
"""能力注册表：catalog 登记校验 / 实验性旁路 / surface 分表 / 声明裁剪 / 订阅释放。"""
import inspect
from types import SimpleNamespace

import pytest


@pytest.fixture()
def reg():
    from app.plugins.capabilities.registry import CapabilityRegistry

    r = CapabilityRegistry()
    yield r
    r.clear()


def test_register_unknown_requires_experimental(reg):
    with pytest.raises(KeyError) as ei:
        reg.register("nope.thing", lambda h: 1)
    assert "未在 catalog 登记" in str(ei.value)


def test_register_unknown_experimental_warns(reg, log_capture):
    reg.register("nope.thing", lambda h: 1, experimental=True)
    assert "nope.thing" in reg.registered()
    assert any("[CapabilityExperimental]" in r and "nope.thing" in r for r in log_capture)


def test_register_known_no_warning(reg, log_capture):
    reg.register("session.read", lambda h: "x")
    assert "session.read" in reg.registered()
    assert not any("[CapabilityExperimental]" in r for r in log_capture)


def test_same_cap_two_surfaces(reg):
    reg.register("model.config", lambda h: "ui-cfg", surface="ui")
    reg.register("model.config", lambda h: "tool-cfg", surface="tool")
    ui_pool = reg.build_pool(surface="ui", host="HOST", plugin="p", declared={"model.config"})
    tool_pool = reg.build_pool(surface="tool", host="HOST", plugin="p", declared={"model.config"})
    assert ui_pool["model.config"] == "ui-cfg"
    assert tool_pool["model.config"] == "tool-cfg"


def test_surface_isolation(reg):
    reg.register("mcp.call", lambda h: "m", surface="tool")
    ui_pool = reg.build_pool(surface="ui", host=None, plugin="p", declared={"mcp.call"})
    assert "mcp.call" not in ui_pool


def test_declared_none_grants_all(reg):
    reg.register("session.read", lambda h: 1)
    reg.register("session.write", lambda h: 2)
    pool = reg.build_pool(surface="ui", host=None, plugin="p", declared=None)
    assert set(pool.names()) == {"session.read", "session.write"}


def test_declared_empty_keeps_only_ungated(reg):
    reg.register("state.kv", lambda h: {}, surface="ui")
    reg.register("session.read", lambda h: 1, surface="ui")
    pool = reg.build_pool(surface="ui", host=None, plugin="p", declared=set())
    assert pool.names() == ["state.kv"]


def test_two_arg_provider_arity_detected(reg):
    def provider(host, ctx):
        return ctx.plugin

    reg.register("plugin.log", provider)
    pool = reg.build_pool(surface="ui", host=None, plugin="demo", declared={"plugin.log"})
    assert pool["plugin.log"] == "demo"
    assert inspect.signature(provider).parameters.__len__() == 2


def test_release_plugin_disconnects_tracked():
    from app.plugins.capabilities.registry import CapabilityRegistry

    class _Sig:
        def __init__(self):
            self.disconnected = []

        def disconnect(self, slot):
            self.disconnected.append(slot)

    r = CapabilityRegistry()
    sig = _Sig()
    r.track_subscription("demo", sig, "CALLBACK")
    assert r.release_plugin("demo") == 1
    assert sig.disconnected == ["CALLBACK"]
    assert r.release_plugin("demo") == 0


def test_release_plugin_tolerates_disconnect_errors():
    from app.plugins.capabilities.registry import CapabilityRegistry

    class _Bad:
        def disconnect(self, slot):
            raise RuntimeError("已断开")

    r = CapabilityRegistry()
    r.track_subscription("demo", _Bad(), object())
    assert r.release_plugin("demo") == 0


def test_reset_instance():
    from app.plugins.capabilities.registry import CapabilityRegistry

    a = CapabilityRegistry.get_instance()
    CapabilityRegistry.reset_instance()
    b = CapabilityRegistry.get_instance()
    assert a is not b
    b.clear()

# ── S2：_entries_version 三点递增 + clear 三缓存同清 ──

def test_entries_version_increments_on_register_unregister_clear(reg):
    """M2-D1：register/unregister/clear 三点各恰 +1（不是 +2 或不变）。"""
    v0 = reg._entries_version
    reg.register("session.read", lambda h: 1)
    assert reg._entries_version == v0 + 1
    reg.unregister("session.read")
    assert reg._entries_version == v0 + 2
    reg.clear()
    assert reg._entries_version == v0 + 3


def test_clear_purges_hostless_pools_audited_and_specs(reg):
    """M2-D2：clear 后 hostless 池/全局审计/池规格/声明缓存四容器全空。

    预先制造三类条目：host=None 显式声明产 _hostless_pools；pool[cap] 首触
    产 registry 侧 _audited 键；host 非空 build_pool 产 _pool_specs。
    """
    reg.register("session.read", lambda h: 1)
    pool_h = reg.build_pool(surface="ui", host=None, plugin="demo", declared={"session.read"})
    assert reg._hostless_pools
    pool_h["session.read"]  # 首触：registry 侧 _audited 记 (plugin,surface,cap)
    assert ("demo", "ui", "session.read") in reg._audited
    reg.build_pool(surface="ui", host="WIN", plugin="demo", declared={"session.read"})
    assert reg._pool_specs

    reg.clear()
    assert not reg._hostless_pools
    assert not reg._audited
    assert not reg._pool_specs
    assert not reg._declared_cache


def test_stale_hostless_pool_not_served_after_clear(reg):
    """M2-F1（失效路径）：clear 后同签名 build_pool 不得复用旧池，且审计重新记账。"""
    key = ("demo", "ui", "session.read")
    reg.register("session.read", lambda h: 1)
    p1 = reg.build_pool(surface="ui", host=None, plugin="demo", declared={"session.read"})
    p2 = reg.build_pool(surface="ui", host=None, plugin="demo", declared={"session.read"})
    assert p1 is p2  # hostless 缓存命中
    p1["session.read"]
    assert key in reg._audited

    reg.clear()
    assert key not in reg._audited  # 去重集合真被清，非表面空 dict

    # clear 后重建注册面再取池：必须是新实例，且首触审计重新记账
    reg.register("session.read", lambda h: 1)
    p3 = reg.build_pool(surface="ui", host=None, plugin="demo", declared={"session.read"})
    assert p3 is not p1
    assert key not in reg._audited
    p3["session.read"]
    assert key in reg._audited
