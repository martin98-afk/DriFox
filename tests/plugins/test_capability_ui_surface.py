# -*- coding: utf-8 -*-
"""UI 面：ctx["capabilities"] 注入 / window.* / 未声明时全量 / 声明后按清单裁 / 卸载释放。"""
from types import SimpleNamespace

import pytest


class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        if slot not in self.slots:
            raise TypeError("未连接")
        self.slots.remove(slot)

    def emit(self, *a):
        for s in list(self.slots):
            s(*a)


class _Tm:
    def __init__(self):
        self.activeTabChanged = _Sig()
        self._windows = [SimpleNamespace(_window_id="w1"), SimpleNamespace(_window_id="w2")]

    def get_current_window(self):
        return self._windows[0]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    yield
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()


def _build(monkeypatch, declared, tm):
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry
    import app.plugins.capabilities.providers.ui_core as ui_core

    ensure_registered("ui")
    host = SimpleNamespace(backend=None, _window_id="w1", window=lambda: "W")
    reg = CapabilityRegistry.get_instance()
    monkeypatch.setattr(ui_core, "_resolve_tab_window", lambda: tm)
    return reg.build_pool(surface="ui", host=host, plugin="demo", declared=declared), host


def test_window_active_and_list(monkeypatch):
    tm = _Tm()
    pool, _ = _build(monkeypatch, {"window.active", "window.list"}, tm)
    assert pool["window.active"]()._window_id == "w1"
    assert pool["window.list"]() == {"w1", "w2"}


def test_window_events_tracked_and_released(monkeypatch):
    from app.plugins.capabilities.registry import CapabilityRegistry

    tm = _Tm()
    pool, _ = _build(monkeypatch, {"window.events"}, tm)
    got = []
    pool["window.events"](lambda p: got.append(p))
    tm.activeTabChanged.emit({"tab_index": 1})
    assert got == [{"tab_index": 1}]
    CapabilityRegistry.get_instance().release_plugin("demo")
    tm.activeTabChanged.emit({"tab_index": 2})
    assert got == [{"tab_index": 1}]


def test_host_raw_only_when_declared(monkeypatch):
    tm = _Tm()
    pool, host = _build(monkeypatch, {"host.raw"}, tm)
    assert pool["host.raw"] is host


def test_compat_period_grants_all_registered(monkeypatch):
    tm = _Tm()
    pool, _ = _build(monkeypatch, None, tm)
    assert {"window.active", "window.events", "ui.notify"} <= set(pool.names())


def test_ui_context_injects_capabilities():
    """_build_ui_context 的返回值必须带 capabilities 键（源码级守卫，避免重型窗口构造）。"""
    import inspect

    import app.main_widget as mw

    src = inspect.getsource(mw.OpenAIChatToolWindow._build_ui_context)
    assert '"capabilities"' in src


def test_unload_releases_capability_subs():
    """unload_plugin 必须调用 release_plugin（源码级守卫）。"""
    import inspect

    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    src = inspect.getsource(UIPluginRegistry.unload_plugin)
    assert "release_plugin" in src
