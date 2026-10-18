# -*- coding: utf-8 -*-
"""UI services 投影：旧键全部由能力池渲染，键集与 EngineHost 一致。"""
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


def _fake_host():
    """覆盖 ui_core 全部 provider 需要的最小 host 面。"""
    from types import SimpleNamespace

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


def _project(declared):
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.catalog import CATALOG, LEGACY_MAP
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered("ui")
    host = _fake_host()
    pool = CapabilityRegistry.get_instance().build_pool(
        surface="ui", host=host, plugin="demo", declared=declared
    )
    return {
        old: pool.legacy(old, cap)
        for old, cap in LEGACY_MAP.items()
        if cap in pool and "ui" in CATALOG[cap].surfaces
    }


def test_full_projection_covers_engine_host_keys():
    from app.plugins.contracts.engine_host import EngineHost

    projected = _project(None)
    contract = {
        name
        for name in dir(EngineHost)
        if not name.startswith("_") and callable(getattr(EngineHost, name, None))
    }
    assert contract <= set(projected), f"缺键: {contract - set(projected)}"


def test_declared_trims_projection():
    """声明清单只留 ui.notify + 恒放行的 state.kv（window_state 旧键）。"""
    projected = _project({"ui.notify"})
    assert set(projected) == {"notify", "window_state"}


def test_empty_declared_projects_only_ungated():
    """空清单：只剩无权限元能力（state.kv 的旧键 window_state 仍投影）。"""
    projected = _project(set())
    assert set(projected) == {"window_state"}


def test_build_ui_services_is_projection_not_implementation():
    """_build_ui_services 必须是投影（不得再手写服务实现，源码级守卫）。"""
    import inspect

    import app.main_widget as mw

    src = inspect.getsource(mw.OpenAIChatToolWindow._build_ui_services)
    assert "LEGACY_MAP" in src
    # 旧实现的特征：手写 dict 取值。投影后不应再出现这些键的字符串字面量赋值
    assert '"get_provider_config":' not in src
    assert '"create_engine_session":' not in src


def test_workdir_write_is_semantic_action():
    """workdir.write 必须封装多步内部细节（不是把 set_workdir 搬家）。"""
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    calls = []
    host = SimpleNamespace(
        _current_project="proj",
        _current_workdir={},
        backend=SimpleNamespace(
            tool_executor=SimpleNamespace(set_workdir=lambda p: calls.append(("exec", p)))
        ),
        _broadcast_team_workdir=lambda p: calls.append(("team", p)),
        _sync_working_directory=lambda: calls.append(("sync", None)),
    )
    ensure_registered("ui")
    pool = CapabilityRegistry.get_instance().build_pool(
        surface="ui", host=host, plugin="demo", declared={"workdir.write"}
    )
    pool["workdir.write"]("/tmp/x")
    assert host._current_workdir["proj"] == "/tmp/x"
    assert ("exec", "/tmp/x") in calls
    assert ("team", "/tmp/x") in calls
    assert ("sync", None) in calls
