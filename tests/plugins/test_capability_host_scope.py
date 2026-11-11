# -*- coding: utf-8 -*-
"""capability host 作用域：多标签下必须解析到「活跃窗口」。

回归背景（2026-10-30）：
- 历史会话页（``history-manager``，container="left" 的全局浮动卡）挂在 Tab 窗口
  的 GLOBAL_WINDOW_ID 作用域，能力池 host 由 ``caps_for`` 动态解析。
- 旧解析链：``_plugin_hosts[plugin]`` → ``UIPluginRegistry._main_widget``。
  前者在建卡时写一次「当时的活跃窗口」，后者是「最后构造的窗口」= 最新标签页。
  切标签页两者都不更新 → 点历史会话加载进错误标签页（session.manage /
  project.write / team.session / window.current 全部作用于错窗口）。
- 修复：全局作用域卡片不登记 host 绑定；无绑定时解析「活跃窗口」
  （``resolve.active_main_widget``）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture()
def reg(monkeypatch):
    from app.plugins.capabilities import bootstrap, resolve
    from app.plugins.capabilities.registry import CapabilityRegistry

    resolve.reset_hosts()
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    yield CapabilityRegistry.get_instance()
    resolve.reset_hosts()
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()


def _fake_pm(monkeypatch, caps, api_version=2):
    """让 PluginManager.get_plugin 返回指定 manifest。"""
    from pathlib import Path

    from app.plugins.managers import plugin_manager

    info = SimpleNamespace(
        manifest={"capabilities": caps, "api_version": api_version},
        path=str(Path(__file__).resolve().parents[2] / "plugins" / "history-manager"),
    )
    monkeypatch.setattr(plugin_manager.PluginManager, "get_plugin", lambda self, name: info, raising=False)


def _win(wid):
    """替身对话窗口：带 window.current 所需字段 + 可注入的宿主方法。"""
    holder = {}

    def _build_ui_context():
        return {"main_widget": holder["win"], "window_id": wid, "backend": object()}

    win = SimpleNamespace(
        _window_id=wid,
        _current_project="demo",
        _current_workdir={},
        _current_session_id="",
        backend=SimpleNamespace(get_current_session=lambda: None),
        session_manager=None,
        _is_streaming=False,
    )
    win._build_ui_context = _build_ui_context
    holder["win"] = win
    return win


def _install_active(monkeypatch, win):
    """让 active_main_widget 解析到 win（TabManagerWindow.get_current_window）。"""
    import app.widgets.tab_manager_window as tm_mod

    monkeypatch.setattr(
        tm_mod.TabManagerWindow,
        "get_instance",
        classmethod(lambda cls: SimpleNamespace(get_current_window=lambda: win, spawn_tab=None)),
    )


def _card_info(plugin="history-manager"):
    from app.plugins.registries.ui_plugin_registry import FloatingCardInfo

    return FloatingCardInfo(plugin_name=plugin, card_id=plugin, widget_class=object, container="left")


def test_fallback_uses_active_window(reg, monkeypatch):
    """无绑定时 host 必须是活跃窗口，不是最后构造的窗口。"""
    from app.plugins.capabilities import resolve
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    real_reg = UIPluginRegistry.get_instance()
    last_constructed = _win("win-last")  # 最新标签页（最后 __init__）
    active = _win("win-active")  # 用户此刻所在标签页
    monkeypatch.setattr(real_reg, "_main_widget", last_constructed, raising=False)
    _install_active(monkeypatch, active)

    assert resolve._resolve_host("demo", "ui") is active


def test_global_scope_card_does_not_bind_host(reg, monkeypatch):
    """全局作用域卡片（GLOBAL_WINDOW_ID）不登记插件→窗口绑定。

    绑定一旦落库就冻结在「建卡时的活跃窗口」，之后切标签页仍按旧窗口解析 host。
    """
    from app.plugins.capabilities.resolve import _plugin_hosts
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
    from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID

    real_reg = UIPluginRegistry.get_instance()
    win_a = _win("win-a")
    monkeypatch.setattr(real_reg, "_context_providers", {"win-a": win_a._build_ui_context}, raising=False)
    _install_active(monkeypatch, win_a)

    ctx = real_reg._build_card_context(_card_info(), GLOBAL_WINDOW_ID)

    assert ctx.get("main_widget") is win_a, "全局卡 ctx 应投影到活跃窗口"
    assert "history-manager" not in _plugin_hosts, "全局卡不得登记插件→窗口绑定"


def test_per_window_card_binds_host(reg, monkeypatch):
    """窗口级卡片（注册了专属 window_id provider）仍登记绑定，语义不变。"""
    from app.plugins.capabilities.resolve import _plugin_hosts
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    real_reg = UIPluginRegistry.get_instance()
    win_a = _win("win-a")
    monkeypatch.setattr(real_reg, "_context_providers", {"win-a": win_a._build_ui_context}, raising=False)

    real_reg._build_card_context(_card_info(), "win-a")

    assert _plugin_hosts.get("history-manager") is win_a


def test_session_manage_select_targets_active_window(reg, monkeypatch):
    """history-manager 的 _caps("session.manage").select 必须加载进活跃窗口。"""
    from app.plugins.capabilities import resolve
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
    from app.utils.history_manager import HistoryManager

    _fake_pm(monkeypatch, ["session.manage"])
    real_reg = UIPluginRegistry.get_instance()
    active = _win("win-active")
    loaded = []
    active._load_session_from_record = loaded.append
    last_constructed = _win("win-last")
    monkeypatch.setattr(real_reg, "_main_widget", last_constructed, raising=False)
    _install_active(monkeypatch, active)
    record = {"session_id": "s-1", "title": "t"}
    monkeypatch.setattr(
        HistoryManager,
        "get_instance",
        classmethod(lambda cls: SimpleNamespace(get_session_by_session_id=lambda sid: record)),
        raising=False,
    )

    api = resolve.caps_for("session.manage", plugin="history-manager")

    assert api is not None and api["select"]("s-1") is True
    assert loaded == [record], "会话应加载进活跃窗口"


def test_session_manage_select_streaming_spawns_tab(reg, monkeypatch):
    """流式输出时点历史会话：开新标签页，不打断当前对话。

    契约来自 ``_session_manage`` 的 docstring（「含流式保护开新标签页」），
    实现曾漏掉——插件历史页直接调 loader 会把当前流式对话停掉。
    """
    import app.widgets.tab_manager_window as tm_mod
    from app.plugins.capabilities import resolve
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
    from app.utils.history_manager import HistoryManager

    _fake_pm(monkeypatch, ["session.manage"])
    real_reg = UIPluginRegistry.get_instance()
    active = _win("win-active")
    active._is_streaming = True
    loaded = []
    active._load_session_from_record = loaded.append
    monkeypatch.setattr(real_reg, "_main_widget", active, raising=False)
    spawned = []

    def _spawn_tab(source, **kwargs):
        spawned.append((source, kwargs))
        return object()

    monkeypatch.setattr(
        tm_mod.TabManagerWindow,
        "get_instance",
        classmethod(lambda cls: SimpleNamespace(get_current_window=lambda: active, spawn_tab=_spawn_tab)),
    )
    record = {"session_id": "s-1", "title": "t"}
    monkeypatch.setattr(
        HistoryManager,
        "get_instance",
        classmethod(lambda cls: SimpleNamespace(get_session_by_session_id=lambda sid: record)),
        raising=False,
    )

    api = resolve.caps_for("session.manage", plugin="history-manager")

    assert api is not None and api["select"]("s-1") is True
    assert spawned and spawned[0][1].get("session_record") is record
    assert loaded == [], "流式路径不得原地加载（会打断当前对话）"
