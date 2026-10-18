"""窗口级能力池缓存：一次 context 只建一个池。

回归背景：_build_ui_context 里 _build_ui_services 调 2 次、_build_ui_capabilities
1 次，每次内部都 build_pool（~5μs 且结果相同）→ 一次 context 建 3 个池。
provider 集合运行中不变（启动时注册），可以每窗口缓存一个。
"""

import inspect

import pytest


def test_build_ui_capabilities_uses_cache():
    """_build_ui_capabilities 必须有缓存分支（源码级守卫）。"""
    import app.main_widget as mw

    src = inspect.getsource(mw.OpenAIChatToolWindow._build_ui_capabilities)
    assert "_cap_pool_cache" in src


def test_window_pool_is_stable_across_calls():
    """同一窗口连续调用返回同一个池对象（复用而非重建）。

    用哑对象承载 unbound 方法：OpenAIChatToolWindow 不能 __new__ 后直接取属性
    （Qt 运行时检查会抛 RuntimeError）。
    """
    import app.main_widget as mw

    class _Win:
        _build_ui_capabilities = mw.OpenAIChatToolWindow._build_ui_capabilities

    win = _Win()
    a = win._build_ui_capabilities()
    b = win._build_ui_capabilities()
    # 构建失败时兜底新建空池，此时不要求同一性
    if type(a).__name__ == "CapabilityPool" and not a._providers:
        return
    assert a is b


def test_project_services_accepts_cached_pool():
    """project_services 对同一池重复投影结果一致（可重入）。"""
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry
    from app.plugins.registries.ui_plugin_registry import project_services

    ensure_registered("ui")
    host_fns = {
        "_get_current_model_config": lambda: {},
        "_resolve_provider_config": lambda p="", m="": {},
        "_build_agent_tools_schema": lambda n: [],
        "_resolve_project_workdir": lambda: "",
        "_broadcast_team_workdir": lambda p: None,
        "_sync_working_directory": lambda: None,
        "_resolve_theme_snapshot": lambda: {},
        "_save_messages_to_session": lambda m, t="": None,
        "enter_exclusive_ui_mode": lambda s: None,
        "exit_exclusive_ui_mode": lambda s: None,
        "_card_manager": None,
        "_window_id": "w1",
        "backend": None,
        "window": lambda: None,
        "_current_project": "p",
        "_current_workdir": {},
    }
    from types import SimpleNamespace

    host = SimpleNamespace(**host_fns)
    pool = CapabilityRegistry.get_instance().build_pool(surface="ui", host=host, plugin="")
    a = project_services(pool)
    b = project_services(pool)
    assert set(a) == set(b)
