# -*- coding: utf-8 -*-
"""UI 补充能力：window.current（语义快照）/ window.workbench / ui.events /
command.* / plugin.control。"""
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    yield
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()


def _pool(declared, monkeypatch, tm=None, host=None):
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry
    import app.plugins.capabilities.providers.ui_core as ui_core

    ensure_registered("ui")
    if tm is not None:
        monkeypatch.setattr(ui_core, "_resolve_tab_window", lambda: tm)
    if host is None:
        host = SimpleNamespace(_window_id="w1", window=lambda: "W", backend=None)
    return CapabilityRegistry.get_instance().build_pool(
        surface="ui", host=host, plugin="demo", declared=declared
    )


# ──────────────── window.current（语义快照，不给裸窗口） ────────────────


class _Tm:
    def __init__(self, win=None):
        self._win = win
        self.activeTabChanged = _Sig()
        self.refreshed = 0
        self.opened = []

    def get_current_window(self):
        return self._win


class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, s):
        self.slots.append(s)

    def disconnect(self, s):
        if s not in self.slots:
            raise TypeError("未连接")
        self.slots.remove(s)

    def emit(self, *a):
        for s in list(self.slots):
            s(*a)


def test_window_current_returns_semantic_snapshot(monkeypatch):
    """关键：不给裸窗口对象，只给语义字段（否则插件仍会摸 _current_*）。"""
    win = SimpleNamespace(
        backend="BE",
        _current_session_id="s1",
        _current_project="proj",
        _current_workdir={"proj": "/tmp/x"},
    )
    pool = _pool({"window.current"}, monkeypatch, tm=_Tm(win))
    snap = pool["window.current"]()
    assert snap == {
        "window_id": "",
        "backend": "BE",
        "session_id": "s1",
        "project": "proj",
        "workdir": "/tmp/x",
    }
    # 不得暴露窗口对象本身
    assert not any(v is win for v in snap.values())


def test_window_current_none_when_no_window(monkeypatch):
    pool = _pool({"window.current"}, monkeypatch, tm=_Tm(None))
    assert pool["window.current"]() is None


def test_window_current_workdir_falls_back_to_resolver(monkeypatch):
    """_current_workdir 无该项目时回落 _resolve_project_workdir()。"""
    win = SimpleNamespace(
        backend=None,
        _current_session_id="",
        _current_project="p",
        _current_workdir={},
        _resolve_project_workdir=lambda: "/tmp/y",
    )
    pool = _pool({"window.current"}, monkeypatch, tm=_Tm(win))
    assert pool["window.current"]()["workdir"] == "/tmp/y"


# ──────────────── window.workbench ────────────────


def test_window_workbench_open_and_refresh(monkeypatch):
    tm = _Tm()
    tm.open_workbench_memory = lambda key: tm.opened.append(key)
    tm.refresh_workbench = lambda: setattr(tm, "refreshed", tm.refreshed + 1)
    pool = _pool({"window.workbench"}, monkeypatch, tm=tm)
    api = pool["window.workbench"]
    api["open"]("docs")
    api["refresh"]()
    assert tm.opened == ["docs"]
    assert tm.refreshed == 1


def test_window_workbench_without_tm_is_noop(monkeypatch):
    pool = _pool({"window.workbench"}, monkeypatch, tm=None)
    api = pool["window.workbench"]
    api["open"]("docs")  # 不应抛异常
    api["refresh"]()


# ──────────────── ui.events ────────────────


def test_ui_events_subscribe_and_release(monkeypatch):
    from app.plugins.capabilities.registry import CapabilityRegistry

    got = []
    pool = _pool({"ui.events"}, monkeypatch)
    pull = pool["ui.events"]()
    pull["subscribe"]("theme_changed", lambda p: got.append(p))
    try:
        from app.core.infra.ui_event_bus import UIEventBus

        UIEventBus.get_instance().publish("theme_changed", theme_id="t1")
        assert got == [{"theme_id": "t1"}]
        CapabilityRegistry.get_instance().release_plugin("demo")
        UIEventBus.get_instance().publish("theme_changed", theme_id="t2")
        assert got == [{"theme_id": "t1"}]  # 已退订
    finally:
        CapabilityRegistry.get_instance().release_plugin("demo")


def test_ui_events_publish(monkeypatch):
    got = []
    pool = _pool({"ui.events"}, monkeypatch)
    pull = pool["ui.events"]()
    try:
        from app.core.infra.ui_event_bus import UIEventBus

        UIEventBus.get_instance().subscribe("cap_probe", lambda p: got.append(p), plugin_name="demo")
        pull["publish"]("cap_probe", k=1)
        assert got == [{"k": 1}]
    finally:
        CapabilityRegistry_cleanup()


def CapabilityRegistry_cleanup():
    from app.core.infra.ui_event_bus import UIEventBus

    UIEventBus.get_instance().unsubscribe_plugin("demo")


# ──────────────── command.* ────────────────


def test_command_read(monkeypatch):
    pool = _pool({"command.read"}, monkeypatch)
    api = pool["command.read"]()
    names = api["all"]()
    assert isinstance(names, (list, dict, set))


def test_command_reload(monkeypatch):
    pool = _pool({"command.reload"}, monkeypatch)
    api = pool["command.reload"]()
    api["reload"]()  # 不应抛异常


# ──────────────── plugin.control ────────────────


def test_plugin_control_watcher_and_reload(monkeypatch):
    pool = _pool({"plugin.control"}, monkeypatch)
    api = pool["plugin.control"]()
    assert set(api) == {"suppress_watcher", "resume_watcher", "reload"}
    api["suppress_watcher"]()
    api["resume_watcher"]()
    api["reload"]("some-plugin")
