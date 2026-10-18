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
