# -*- coding: utf-8 -*-
"""回调类入口（输入按钮/侧栏）的 ctx 按插件声明裁剪。"""
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _fresh():
    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    yield
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()


def _fake_host():
    return SimpleNamespace(
        backend=None,
        window=lambda: "W",
        _window_id="w1",
        _current_project="proj",
        _current_workdir={},
        _card_manager=SimpleNamespace(hide_card=lambda cid, wid: None),
        _get_current_model_config=lambda: {},
        _resolve_provider_config=lambda provider="", model="": {},
        _build_agent_tools_schema=lambda name: [],
        _resolve_project_workdir=lambda: "",
        _broadcast_team_workdir=lambda p: None,
        _sync_working_directory=lambda: None,
        _resolve_theme_snapshot=lambda: {},
        _save_messages_to_session=lambda messages, task_prompt="": None,
        enter_exclusive_ui_mode=lambda sid: None,
        exit_exclusive_ui_mode=lambda sid: None,
    )


def test_callback_context_has_capabilities(monkeypatch):
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.managers.plugin_manager import PluginManager
    from app.plugins.registries.ui_plugin_registry import callback_context

    ensure_registered("ui")
    info = SimpleNamespace(manifest={"capabilities": ["ui.notify"]}, path="")
    monkeypatch.setattr(
        PluginManager,
        "get_instance",
        classmethod(lambda cls: SimpleNamespace(get_plugin=lambda n: info if n == "demo" else None)),
    )
    win = _fake_host()
    ctx = callback_context("demo", win, {"button_id": "b1"})
    assert "capabilities" in ctx
    assert set(ctx["capabilities"].names()) == {"ui.notify", "plugin.log", "state.kv"}
    assert ctx["button_id"] == "b1"
    assert ctx["plugin_name"] == "demo"
    assert ctx["main_widget"] is win
    # services 同步裁剪：只留 ui.notify 的旧键 + 恒放行的 window_state
    assert set(ctx["services"]) == {"notify", "window_state"}


def test_callback_context_falls_back_without_host():
    from app.plugins.registries.ui_plugin_registry import callback_context

    ctx = callback_context("demo", None, {"item_id": "x"})
    assert ctx == {"item_id": "x", "plugin_name": "demo"}


def test_invoke_input_button_uses_callback_context():
    """两个回调入口必须走 callback_context（源码级守卫，避免退回手拼 dict）。"""
    import inspect

    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    for name in ("invoke_input_button", "invoke_sidebar_item"):
        src = inspect.getsource(getattr(UIPluginRegistry, name))
        assert "callback_context(" in src, f"{name} 未走 callback_context"
        assert '"main_widget": win' not in src, f"{name} 仍在手拼 ctx"
