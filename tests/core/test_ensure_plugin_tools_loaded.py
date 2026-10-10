# -*- coding: utf-8 -*-
"""[T22/T17-2] 回归：_ensure_plugin_tools_loaded 失败不置位 + 60s 节流重试。

原实现 try 前置位：首次加载失败后进程内永不重试，工具永久缺失。
新契约：成功才置位；失败记时间戳，60s 内重试被节流，过期后放行。
"""

import time

import pytest

import app.tools as tools_pkg


@pytest.fixture()
def _reset_flags(monkeypatch):
    """复位模块级加载标志/失败时间戳，并桩掉真实加载链。"""
    monkeypatch.setattr(tools_pkg, "_plugin_tools_loaded", False)
    monkeypatch.setattr(tools_pkg, "_plugin_tools_last_fail_ts", 0.0)
    calls = {"load": 0, "watcher": 0}
    monkeypatch.setattr(
        "app.plugins.loaders.plugin_tool_loader.load_plugin_tools",
        lambda: calls.__setitem__("load", calls["load"] + 1) or {"p": {"t"}},
    )
    monkeypatch.setattr(
        "app.plugins.loaders.plugin_tool_loader.ensure_plugin_tool_watcher",
        lambda: calls.__setitem__("watcher", calls["watcher"] + 1),
    )
    return calls


def test_success_sets_flag_and_idempotent(_reset_flags, monkeypatch):
    """成功加载置位：二次调用不再触发加载链（幂等）。"""
    calls = _reset_flags
    tools_pkg._ensure_plugin_tools_loaded()
    assert tools_pkg._plugin_tools_loaded is True
    assert calls["load"] == 1

    tools_pkg._ensure_plugin_tools_loaded()
    assert calls["load"] == 1, "置位后二次调用不得重复加载"


def test_failure_keeps_flag_unset_and_throttles_retry(_reset_flags, monkeypatch):
    """失败不置位；60s 内的重试被节流（不真扫盘）。"""
    calls = _reset_flags

    def _boom():
        raise RuntimeError("plugin file locked")

    monkeypatch.setattr("app.plugins.loaders.plugin_tool_loader.load_plugin_tools", _boom)
    monkeypatch.setattr(tools_pkg.time, "monotonic", lambda: 1000.0)

    tools_pkg._ensure_plugin_tools_loaded()
    assert tools_pkg._plugin_tools_loaded is False, "失败不得置位"
    assert tools_pkg._plugin_tools_last_fail_ts == 1000.0

    # 60s 内：节流直接返回，加载链不再被触发
    monkeypatch.setattr(tools_pkg.time, "monotonic", lambda: 1030.0)
    monkeypatch.setattr("app.plugins.loaders.plugin_tool_loader.load_plugin_tools", lambda: calls.__setitem__("load", calls["load"] + 1) or {})
    tools_pkg._ensure_plugin_tools_loaded()
    assert calls["load"] == 0, "节流窗口内不得重试加载"


def test_retry_allowed_after_throttle_window(_reset_flags, monkeypatch):
    """失败后超过 60s：重试放行；本次成功则置位。"""
    calls = _reset_flags

    def _boom():
        raise RuntimeError("transient")

    monkeypatch.setattr("app.plugins.loaders.plugin_tool_loader.load_plugin_tools", _boom)
    monkeypatch.setattr(tools_pkg.time, "monotonic", lambda: 1000.0)
    tools_pkg._ensure_plugin_tools_loaded()
    assert tools_pkg._plugin_tools_loaded is False

    # 61s 后 + 加载链恢复 → 重试成功置位
    monkeypatch.setattr("app.plugins.loaders.plugin_tool_loader.load_plugin_tools", lambda: calls.__setitem__("load", calls["load"] + 1) or {})
    monkeypatch.setattr(tools_pkg.time, "monotonic", lambda: 1061.0)
    tools_pkg._ensure_plugin_tools_loaded()
    assert tools_pkg._plugin_tools_loaded is True, "节流窗过后重试应放行并置位"
    assert calls["load"] == 1, "节流窗过后重试应放行并置位"


def test_success_after_failure_stops_retry(_reset_flags, monkeypatch):
    """失败→恢复→成功置位后不再重试（失败时间戳不再影响）。"""
    calls = _reset_flags

    monkeypatch.setattr("app.plugins.loaders.plugin_tool_loader.load_plugin_tools", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(tools_pkg.time, "monotonic", lambda: 1000.0)
    tools_pkg._ensure_plugin_tools_loaded()

    monkeypatch.setattr("app.plugins.loaders.plugin_tool_loader.load_plugin_tools", lambda: calls.__setitem__("load", calls["load"] + 1) or {})
    monkeypatch.setattr(tools_pkg.time, "monotonic", lambda: 2000.0)
    tools_pkg._ensure_plugin_tools_loaded()
    assert tools_pkg._plugin_tools_loaded is True

    tools_pkg._ensure_plugin_tools_loaded()
    assert calls["load"] == 1, "成功置位后不再重试"
